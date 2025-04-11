import discord
from discord.ext import commands
from discord.ui import Button, View, Select
import aiosqlite
import random
import asyncio
import logging
import math
from typing import List, Dict, Tuple, Optional, Union, Set

# Import economy system
from luna import not_blacklisted, logger, bot
from commands.economy import economy_system, _parse_amount_shorthand, ECO_DB_PATH

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger("mines")

# --- Constants ---
MIN_BET = 10
MAX_BET = 500000
DEFAULT_BET = 50

# Grid settings
GRID_WIDTH = 3  # Classic gambling mines uses 3x3 grid
GRID_HEIGHT = 3
TOTAL_TILES = GRID_WIDTH * GRID_HEIGHT

# Mine count options
MAX_MINES = TOTAL_TILES - 1 # Must leave at least one safe tile
MIN_MINES = 1

# Emojis
HIDDEN_EMOJI = "⬜"  # Hidden tile
MINE_EMOJI = "💣"   # Mine tile
GEM_EMOJI = "💎"    # Safe tile
DIAMOND_EMOJI = "🔷" # Alternative safe tile
BOOM_EMOJI = "💥"   # Exploded mine

# Track active games
active_games: Dict[str, 'MinesGame'] = {}

# --- Utility Functions ---
async def is_user_premium(user_id: int) -> bool:
    """Check if user has premium status."""
    # Convert to string for economy system compatibility
    return await economy_system.is_premium(str(user_id)) if hasattr(economy_system, "is_premium") else False

# --- Database Functions ---
async def get_user_balance(user_id: int) -> int:
    """Fetches the user's balance, returning 0 if user doesn't exist."""
    try:
        # Convert to string for economy system compatibility
        return await economy_system.get_cash(str(user_id))
    except Exception as e:
        logger.error(f"Error getting user balance: {e}")
        return 0

async def update_user_balance(user_id: int, amount: int) -> int:
    """Updates the user's balance by the given amount. Returns new balance."""
    try:
        # Convert to string for economy system compatibility
        user_id_str = str(user_id)
        if amount > 0:
            return await economy_system.add_cash(user_id_str, amount, "mines_win", f"Mines game win: {amount}")
        elif amount < 0:
            return await economy_system.add_cash(user_id_str, amount, "mines_bet", f"Mines game bet: {abs(amount)}")
        return await economy_system.get_cash(user_id_str)
    except Exception as e:
        logger.error(f"Error updating user balance: {e}")
        return 0

async def record_mines_stats(user_id: int, bet: int, win: int, mines_count: int, tiles_revealed: int):
    """Record game statistics."""
    try:
        # Use the economy database path
        async with aiosqlite.connect(ECO_DB_PATH) as db:
            # Check if game_stats table exists
            async with db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='game_stats'") as cursor:
                if not await cursor.fetchone():
                    # Create table if it doesn't exist
                    await db.execute("""
                        CREATE TABLE IF NOT EXISTS game_stats (
                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                            user_id TEXT NOT NULL,
                            game TEXT NOT NULL,
                            bet INTEGER NOT NULL,
                            win INTEGER NOT NULL,
                            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                        )
                    """)
            
            # Record the stats
            await db.execute("""
                INSERT INTO game_stats (user_id, game, bet, win, timestamp) 
                VALUES (?, 'mines', ?, ?, CURRENT_TIMESTAMP)
            """, (str(user_id), bet, win))
            await db.commit()
            logger.info(f"Recorded mines stats for user {user_id}: bet=💵{bet}, win=💵{win}, mines={mines_count}, revealed={tiles_revealed}")
    except Exception as e:
        logger.error(f"Failed to record mines stats: {e}")

# --- Game Logic Functions ---
def calculate_multiplier(safe_tiles: int, mines_count: int, tiles_revealed: int) -> float:
    """Calculate the multiplier based on game parameters and revealed tiles.
    
    This uses a mathematical formula to ensure betting is fair yet exciting.
    """
    if tiles_revealed == 0:
        return 1.0
    
    # Basic multiplier formula for mines game:
    # (Total Tiles / (Total Tiles - Mines))^revealed
    base = TOTAL_TILES / (TOTAL_TILES - mines_count)
    multiplier = base ** tiles_revealed
    
    # Apply house edge (5%)
    house_edge = 0.95  
    multiplier *= house_edge
    
    # Cap max multiplier for safety (prevent economy-breaking wins)
    max_multiplier = 1000.0
    multiplier = min(multiplier, max_multiplier)
    
    return multiplier

# --- UI Components ---
class MineTile(Button):
    """Button representing a tile in the mines game"""
    def __init__(self, x: int, y: int, disabled: bool = False):
        super().__init__(
            style=discord.ButtonStyle.secondary, 
            label=" ",
            emoji=HIDDEN_EMOJI,
            disabled=disabled,
            row=y  # Position buttons in rows
        )
        self.x = x
        self.y = y
        self.is_mine = False
        self.revealed = False
    
    def mark_as_mine(self):
        """Mark this tile as containing a mine"""
        self.is_mine = True
    
    def reveal(self, is_boom: bool = False):
        """Reveal this tile's contents"""
        self.revealed = True
        if self.is_mine:
            if is_boom:
                self.emoji = BOOM_EMOJI
                self.style = discord.ButtonStyle.danger
            else:
                self.emoji = MINE_EMOJI
                self.style = discord.ButtonStyle.danger
        else:
            self.emoji = GEM_EMOJI
            self.style = discord.ButtonStyle.success
            
class MinesView(View):
    """Main view for the Mines game with grid of buttons"""
    def __init__(self, game_ref: 'MinesGame', user_id: int, bet_amount: int, mines_count: int):
        super().__init__(timeout=180)  # 3 minute timeout
        self.game = game_ref
        self.user_id = user_id
        self.bet = bet_amount
        self.mines_count = mines_count
        self.message: Optional[discord.Message] = None
        self.tiles: List[List[MineTile]] = []
        self.game_over = False
        self.won = False
        self.tiles_revealed = 0
        self.safe_tiles = TOTAL_TILES - mines_count
        self.current_multiplier = 1.0
        self.potential_win = bet_amount
        
        # Create the grid
        self._create_grid()
        
        # Add cashout button (bottom row)
        self._add_control_buttons()
        
        # Set up mine positions
        self._setup_mines()
    
    def _create_grid(self):
        """Create the grid of tiles"""
        self.tiles = []
        
        for x in range(GRID_WIDTH):
            column = []
            for y in range(GRID_HEIGHT):
                tile = MineTile(x, y)
                self.add_item(tile)
                column.append(tile)
            self.tiles.append(column)
            
    def _add_control_buttons(self):
        """Add cashout and exit buttons"""
        # Add buttons on a new row
        cashout_button = Button(
            style=discord.ButtonStyle.success,
            label="💰 CASH OUT",
            custom_id="mines_cashout",
            row=GRID_HEIGHT  # Place on the row after the grid
        )
        cashout_button.callback = self.cashout_callback
        
        exit_button = Button(
            style=discord.ButtonStyle.danger,
            label="❌ EXIT",
            custom_id="mines_exit",
            row=GRID_HEIGHT  # Place on the row after the grid
        )
        exit_button.callback = self.exit_callback
        
        self.add_item(cashout_button)
        self.add_item(exit_button)
    
    def _setup_mines(self):
        """Randomly place mines in the grid"""
        # Randomly select tiles to be mines
        mine_positions = random.sample(range(TOTAL_TILES), self.mines_count)
        
        # Mark those tiles as mines
        for pos in mine_positions:
            x = pos % GRID_WIDTH
            y = pos // GRID_WIDTH
            self.tiles[x][y].mark_as_mine()
    
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        """Only allow the original player to use the buttons."""
        if str(interaction.user.id) != str(self.user_id):
            await interaction.response.send_message("This is not your game!", ephemeral=True)
            return False
        return True
    
    async def on_timeout(self):
        """Handle timeout - game is abandoned"""
        if not self.game_over:
            # Auto-cashout if player revealed at least one tile
            if self.tiles_revealed > 0:
                await self._process_cashout(timeout=True)
            elif self.tiles_revealed == 0:
                # Refund bet if no tiles were revealed
                await update_user_balance(self.user_id, self.bet)
                
                # Create timeout embed
                timeout_embed = discord.Embed(
                    title="💎 Mines Game - Timed Out",
                    description=f"Game canceled due to inactivity. Your bet of 💵 {self.bet:,} has been refunded.",
                    color=discord.Color.light_grey()
                )
                
                # Reveal all tiles
                for col in self.tiles:
                    for tile in col:
                        if not tile.revealed:
                            tile.reveal()
                        tile.disabled = True
                
                # Update message if possible
                if self.message:
                    try:
                        await self.message.edit(embed=timeout_embed, view=self)
                    except Exception as e:
                        logger.error(f"Error updating message on timeout: {e}")
            
            # Mark game as over
            self.game_over = True
            
            # Remove from active games
            if str(self.user_id) in active_games:
                active_games.pop(str(self.user_id), None)
    
    async def update_display(self, interaction: Optional[discord.Interaction] = None):
        """Update the game display with current state"""
        # Create updated embed
        embed = self._create_game_embed()
        
        # Update message
        try:
            if interaction and not interaction.response.is_done():
                await interaction.response.edit_message(embed=embed, view=self)
            elif interaction and interaction.response.is_done():
                try:
                    await interaction.followup.edit_message(message_id=interaction.message.id, embed=embed, view=self)
                except:
                    # Fallback to editing original message
                    if self.message:
                        await self.message.edit(embed=embed, view=self)
            elif self.message:
                await self.message.edit(embed=embed, view=self)
        except Exception as e:
            logger.error(f"Error updating display: {e}")
    
    def _create_game_embed(self) -> discord.Embed:
        """Create the game status embed based on current state"""
        if self.game_over:
            # Game over embed
            if self.won:
                color = discord.Color.green()
                title = "💰 You Won!"
                description = f"You made it out with 💵 {self.potential_win:,}!"
            else:
                color = discord.Color.red()
                title = "💥 BOOM! Game Over"
                description = f"You hit a mine and lost 💵 {self.bet:,}!"
        else:
            # Game in progress
            color = discord.Color.gold()
            title = "💎 Mines Game"
            description = (
                f"Reveal gems to win! Avoid the mines!\n"
                f"Current multiplier: **{self.current_multiplier:.2f}x**\n"
                f"Potential win: 💵 **{self.potential_win:,}**"
            )
        
        embed = discord.Embed(
            title=title,
            description=description,
            color=color
        )
        
        # Add game stats as fields
        embed.add_field(
            name="Game Info", 
            value=(
                f"Bet Amount: 💵 {self.bet:,}\n"
                f"Mines: {self.mines_count}/{TOTAL_TILES}\n"
                f"Safe Tiles: {self.tiles_revealed}/{self.safe_tiles} revealed"
            ),
            inline=True
        )
        
        # Add a field explaining controls if game is active
        if not self.game_over:
            embed.add_field(
                name="Controls",
                value=(
                    f"• Click a tile to reveal\n"
                    f"• 💰 Cash Out to secure winnings\n"
                    f"• ❌ Exit to quit the game"
                ),
                inline=True
            )
        
        return embed
    
    async def process_tile_click(self, interaction: discord.Interaction, x: int, y: int):
        """Process a tile click at the given coordinates"""
        # Ignore if game is over
        if self.game_over:
            await interaction.response.defer()
            return
        
        tile = self.tiles[x][y]
        
        # Ignore if already revealed
        if tile.revealed:
            await interaction.response.defer()
            return
        
        # Check if it's a mine
        if tile.is_mine:
            # Game over - hit a mine!
            tile.reveal(is_boom=True)
            
            # Reveal all other mines
            for col in self.tiles:
                for t in col:
                    if t.is_mine and not t.revealed:
                        t.reveal()
            
            # Mark game as over with loss
            self.game_over = True
            self.won = False
            # Disable all tiles immediately on loss
            for col_loss in self.tiles:
                 for tile_loss in col_loss:
                     tile_loss.disabled = True
            
            # Record stats
            await record_mines_stats(
                self.user_id, 
                self.bet, 
                0,  # No winnings
                self.mines_count,
                self.tiles_revealed
            )
            
            # Update display
            await self.update_display(interaction)
            
            # Remove from active games
            if str(self.user_id) in active_games:
                active_games.pop(str(self.user_id), None)
        else:
            # Safe tile! Reveal it
            tile.reveal()
            self.tiles_revealed += 1
            
            # Update multiplier
            self.current_multiplier = calculate_multiplier(
                self.safe_tiles, 
                self.mines_count, 
                self.tiles_revealed
            )
            
            # Update potential win
            self.potential_win = int(self.bet * self.current_multiplier)
            
            # Check if all safe tiles are revealed (perfect game)
            if self.tiles_revealed == self.safe_tiles:
                await self._handle_victory(interaction)
            else:
                # Continue game
                await self.update_display(interaction)
    
    async def _handle_victory(self, interaction: discord.Interaction):
        """Handle player successfully revealing all safe tiles"""
        self.game_over = True
        self.won = True
        
        # Final payout is at max multiplier
        final_payout = self.potential_win
        
        # Update user balance
        # Ensure payout doesn't exceed reasonable limits (safety check)
        final_payout = min(final_payout, self.bet * 1000) # Cap win at 1000x bet
        await update_user_balance(self.user_id, final_payout)
        
        # Record stats
        await record_mines_stats(
            self.user_id, 
            self.bet, 
            final_payout,
            self.mines_count,
            self.tiles_revealed
        )
        
        # Create victory embed
        victory_embed = discord.Embed(
            title="🏆 Perfect Game! All Safe Tiles Found!",
            description=f"You revealed all safe tiles and won 💵 **{final_payout:,}**!",
            color=discord.Color.gold()
        )
        
        # Add game details
        victory_embed.add_field(name="Multiplier", value=f"{self.current_multiplier:.2f}x", inline=True)
        victory_embed.add_field(name="Mines", value=f"{self.mines_count}/{TOTAL_TILES}", inline=True)
        victory_embed.add_field(name="Winnings", value=f"💵 {final_payout:,}", inline=True)
        
        # Disable all buttons
        for child in self.children:
            child.disabled = True
        
        # Update message
        if interaction and not interaction.response.is_done():
            await interaction.response.edit_message(embed=victory_embed, view=self)
        elif self.message:
            await self.message.edit(embed=victory_embed, view=self)
        
        # Remove from active games
        if str(self.user_id) in active_games:
            active_games.pop(str(self.user_id), None)
    
    async def _process_cashout(self, timeout: bool = False, interaction: Optional[discord.Interaction] = None):
        """Process a cashout request"""
        # Can only cashout if game is active and at least one tile revealed
        if self.game_over or self.tiles_revealed == 0:
            if interaction:
                await interaction.response.send_message("Cannot cashout right now!", ephemeral=True)
            return
        
        # Calculate winnings
        winnings = self.potential_win
        
        # Update balance
        await update_user_balance(self.user_id, winnings)
        
        # Record stats
        await record_mines_stats(
            self.user_id, 
            self.bet, 
            winnings,
            self.mines_count,
            self.tiles_revealed
        )
        
        # Mark game as over with win
        self.game_over = True
        self.won = True
        
        # Create cashout embed
        cashout_embed = discord.Embed(
            title="💰 Cashed Out!",
            description=f"You cashed out with a {self.current_multiplier:.2f}x multiplier and won 💵 **{winnings:,}**!",
            color=discord.Color.green()
        )
        
        # Add game details
        cashout_embed.add_field(name="Safe Tiles", value=f"{self.tiles_revealed}/{self.safe_tiles}", inline=True)
        cashout_embed.add_field(name="Multiplier", value=f"{self.current_multiplier:.2f}x", inline=True)
        cashout_embed.add_field(name="Winnings", value=f"💵 {winnings:,}", inline=True)
        
        # Reveal all mine positions
        for col in self.tiles:
            for tile in col:
                if tile.is_mine and not tile.revealed:
                    # Just mark mines with standard mine emoji
                    tile.reveal(is_boom=False)
                # Disable all tiles
                tile.disabled = True
        
        # Disable cashout button
        for child in self.children:
            if hasattr(child, 'custom_id') and child.custom_id in [
                "mines_cashout", "mines_exit"
            ]:
                child.disabled = True
        
        # Update message
        if interaction and not interaction.response.is_done():
            await interaction.response.edit_message(embed=cashout_embed, view=self)
        elif interaction and interaction.response.is_done():
            try:
                await interaction.followup.edit_message(message_id=interaction.message.id, embed=cashout_embed, view=self)
            except:
                # Fallback to editing original message
                if self.message:
                    await self.message.edit(embed=cashout_embed, view=self)
        elif self.message:
            await self.message.edit(embed=cashout_embed, view=self)
        
        # Remove from active games
        if str(self.user_id) in active_games:
            active_games.pop(str(self.user_id), None)
    
    async def cashout_callback(self, interaction: discord.Interaction):
        """Handle cashout button click"""
        await self._process_cashout(interaction=interaction)
    
    async def exit_callback(self, interaction: discord.Interaction):
        """Handle exit button click"""
        # If player hasn't revealed any tiles, refund bet
        if self.tiles_revealed == 0 and not self.game_over:
            # Refund bet
            await update_user_balance(self.user_id, self.bet)
            refund_message = f"Game canceled. Your bet of 💵 {self.bet:,} has been refunded."
        else:
            refund_message = "Game exited."
        
        # Create exit embed
        exit_embed = discord.Embed(
            title="💎 Mines Game - Exited",
            description=refund_message,
            color=discord.Color.light_grey()
        )
        
        # Reveal all tiles
        for col in self.tiles:
            for tile in col:
                if not tile.revealed:
                    tile.reveal()
                tile.disabled = True
        
        # Disable all control buttons
        for child in self.children:
            if hasattr(child, 'custom_id') and child.custom_id in [
                "mines_cashout", "mines_exit"
            ]:
                child.disabled = True
        
        # Update message
        try:
            await interaction.response.edit_message(embed=exit_embed, view=self)
        except discord.errors.InteractionResponded:
             await interaction.followup.edit_message(message_id=interaction.message.id, embed=exit_embed, view=self)
        except Exception as e:
            logger.error(f"Error updating message on exit: {e}")
        
        # Mark game as over
        self.game_over = True
        
        # Remove from active games
        if str(self.user_id) in active_games:
            active_games.pop(str(self.user_id), None)

class MinesCountModal(discord.ui.Modal):
    """Modal for selecting number of mines"""
    def __init__(self, cog, user_id: int, bet_amount: int):
        super().__init__(title="How many mines?")
        self.cog = cog
        self.user_id = user_id
        self.bet_amount = bet_amount
        
        # Add mine count input
        default_mines = 3
        self.mines_input = discord.ui.InputText(
            label=f"Select mines (1-{MAX_MINES})",
            placeholder=f"Default: {default_mines}",
            value=str(default_mines),  # Use value instead of default for py-cord
            required=True,
            min_length=1,
            max_length=1
        )
        self.add_item(self.mines_input)
    
    async def callback(self, interaction: discord.Interaction):
        """Process the modal submission"""
        try:
            # Parse mines count
            mines_count = int(self.mines_input.value)
            # Validate mines count
            if mines_count < MIN_MINES or mines_count > MAX_MINES:
                await interaction.response.send_message(
                    f"Invalid mines count. Must be between {MIN_MINES} and {MAX_MINES}.", 
                    ephemeral=True
                )
                # Refund bet
                await update_user_balance(self.user_id, self.bet_amount)
                return
            
            # Start the game
            await self.cog.start_game(interaction, self.bet_amount, mines_count)
        except ValueError:
            # Not a valid number 
            await interaction.response.send_message("Please enter a valid number for mines count.", ephemeral=True)
            # Refund bet
            await update_user_balance(self.user_id, self.bet_amount)
        except Exception as e:
            logger.error(f"Error processing mines modal: {e}")
            await interaction.response.send_message("An error occurred. Your bet has been refunded.", ephemeral=True)
            # Refund bet
            await update_user_balance(self.user_id, self.bet_amount)

class MinesGame:
    """Class to manage a mines game instance"""
    def __init__(self, bot, channel_id: int, author_id: int):
        self.bot = bot
        self.channel_id = channel_id
        self.author_id = author_id
        self.view = None
        self.bet_amount = 0
    
    async def setup(self, interaction: discord.Interaction, bet_amount: int, mines_count: int):
        """Set up the game with the specified parameters"""
        self.bet_amount = bet_amount
        self.view = MinesView(self, self.author_id, bet_amount, mines_count)
        
        # Create initial game embed
        initial_embed = discord.Embed(
            title="💎 Mines Game",
            description=(
                f"Reveal gems to win! Avoid the mines!\n"
                f"Current multiplier: **1.00x**\n"
                f"Potential win: 💵 **{bet_amount:,}**"
            ),
            color=discord.Color.gold()
        )
        
        # Add game stats as fields
        initial_embed.add_field(
            name="Game Info", 
            value=(
                f"Bet Amount: 💵 {bet_amount:,}\n"
                f"Mines: {mines_count}/{TOTAL_TILES}\n"
                f"Safe Tiles: 0/{TOTAL_TILES - mines_count} revealed"
            ),
            inline=True
        )
        
        # Add a field explaining controls
        initial_embed.add_field(
            name="Controls",
            value=(
                f"• Click a tile to reveal\n"
                f"• 💰 Cash Out to secure winnings\n"
                f"• ❌ Exit to quit the game"
            ),
            inline=True
        )
        
        # Send the game view
        await interaction.response.send_message(embed=initial_embed, view=self.view)
        # Store message reference for updating
        self.view.message = await interaction.original_response()

class MinesCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        # Ensure economy database is initialized
        self.bot.loop.create_task(economy_system.init_db())
        logger.info("MinesCog loaded")
    
    @commands.Cog.listener()
    async def on_ready(self):
        logger.info("MinesCog ready")
        # Set integration types for the mines command to ensure persistence
        for cmd in self.bot.application_commands:
            if cmd.name == "mines":
                from discord import IntegrationType, InteractionContextType
                cmd.integration_types = [IntegrationType.guild_install, IntegrationType.user_install]
                cmd.contexts = {InteractionContextType.guild, InteractionContextType.bot_dm, InteractionContextType.private_channel}
                logger.info("Set integration types for mines command")
    
    @commands.slash_command(name="mines", description="Play a game of Mines to win big!")
    @not_blacklisted()
    async def mines(self, ctx: discord.ApplicationContext, 
                    bet: discord.Option(str, description=f"Amount to bet (Min: 💵{MIN_BET}, Max: 💵{MAX_BET:,}, or use shorthand like 50k)", required=True)):
        """Start a game of Mines"""
        # Get user ID
        user_id = str(ctx.author.id)
        
        # Check if user already has an active game
        if user_id in active_games:
            await ctx.respond("You already have an active Mines game!", ephemeral=True)
            return
        
        # Parse bet amount
        cash = await economy_system.get_cash(user_id)
        if bet.lower() == "all":
            bet_amount = cash
        else:
            try:
                bet_amount = await _parse_amount_shorthand(bet, cash)
                if bet_amount is None:
                    await ctx.respond("Please enter a valid bet amount (number, shorthand like 50k, or 'all').", ephemeral=True)
                    return
            except:
                bet_amount = DEFAULT_BET
        
        # Validate bet amount
        if bet_amount < MIN_BET:
            await ctx.respond(f"Bet amount too low. Minimum bet is 💵 {MIN_BET:,}.", ephemeral=True)
            return
        
        if bet_amount > MAX_BET:
            await ctx.respond(f"Bet cannot exceed 💵 {MAX_BET:,}!", ephemeral=True)
            return
        
        # Check user's balance
        if bet_amount > cash:
            await ctx.respond(f"Not enough balance! Your balance: 💵 {cash:,}", ephemeral=True)
            return
        
        # Deduct bet amount from balance FIRST
        await economy_system.add_cash(user_id, -bet_amount, "mines_bet", f"Mines game bet: {bet_amount}")
        logger.info(f"Deducted 💵{bet_amount} from user {user_id} for Mines game.")

        # Now show the mines count modal
        try:
            mines_modal = MinesCountModal(self, int(user_id), bet_amount)
            await ctx.interaction.response.send_modal(mines_modal) 
            
            # Create a reference to the game
            game = MinesGame(self.bot, ctx.channel.id, int(user_id))
            active_games[user_id] = game
        except Exception as e:
            logger.error(f"Failed to send MinesCountModal for user {user_id}: {e}")
            # Refund bet if modal fails to send
            await economy_system.add_cash(user_id, bet_amount, "mines_refund", f"Mines game refund due to error: {bet_amount}")
            logger.warning(f"Refunded 💵{bet_amount} to user {user_id} due to modal failure.")
            try:
                await ctx.followup.send("Failed to start game setup. Your bet has been refunded.", ephemeral=True)
            except: pass # Ignore if followup fails
    
    async def start_game(self, interaction: discord.Interaction, bet_amount: int, mines_count: int):
        """Start a mines game with the specified parameters"""
        user_id = str(interaction.user.id)
        
        # Get game reference
        if user_id not in active_games:
            await interaction.response.send_message("Game session expired. Your bet has been refunded.", ephemeral=True)
            await economy_system.add_cash(user_id, bet_amount, "mines_refund", "Game session expired refund")
            return
            
        game = active_games[user_id]
        
        # Set up the game
        await game.setup(interaction, bet_amount, mines_count)
        
        # Set up button callbacks
        for col in game.view.tiles:
            for tile in col:
                # We need to create a new function for each button to maintain the coordinates
                async def make_callback(x, y):
                    async def callback(interaction):
                        await game.view.process_tile_click(interaction, x, y)
                    return callback
                
                tile.callback = await make_callback(tile.x, tile.y)
    
    def cog_unload(self):
        """Clean up when cog is unloaded"""
        # Cancel all active games and refund bets if no tiles revealed
        for user_id, game in list(active_games.items()):
            try:
                if game.view and not game.view.game_over and game.view.tiles_revealed == 0:
                    # Refund bet if game hasn't started
                    asyncio.create_task(update_user_balance(int(user_id), game.bet_amount))
            except Exception as e:
                logger.error(f"Error in cog_unload cleanup: {e}")

def setup(bot):
    """Setup function for py-cord extension loading"""
    bot.add_cog(MinesCog(bot))
    logger.info("Mines extension loaded")
