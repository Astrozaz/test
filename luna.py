import discord
from discord.ext import commands
from discord import IntegrationType, InteractionContextType
import os
from dotenv import load_dotenv
import traceback
import sys
import logging
import aiosqlite
import asyncio
import functools

# Configure logging - reduce verbosity by setting level to INFO
logging.basicConfig(
    level=logging.INFO,  # Changed from DEBUG to INFO
    format='%(levelname)s: %(message)s',  # Simplified format
    handlers=[
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger('luna_bot')

# Log basic version info 
logger.info(f"Discord.py version: {discord.__version__}")

# Load environment variables from .env file
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
APP_ID = os.getenv("APP_ID")
OWNER_ID = os.getenv("OWNER_ID")
DB_PATH = os.getenv("DB_PATH", "databases/luna_bot.db")

# ---- Required utility functions used by extensions ----

# Command check for blacklisted users
def not_blacklisted():
    """Check if a user is blacklisted from using the bot"""
    async def predicate(ctx):
        # This is a simplified version for compatibility
        # In a real implementation, you would check a database
        return True
    return commands.check(predicate)

# Command check for bot owner
def is_owner(ctx):
    """Check if the user is the bot owner"""
    return ctx.author.id == int(OWNER_ID) if OWNER_ID else False

# Command check for staff members
async def is_staff(ctx):
    """Check if the user is a staff member"""
    # This is a simplified version for compatibility
    if is_owner(ctx):
        return True
        
    # In a real implementation, you would check a database for staff members
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT user_id FROM staff WHERE user_id = ?", 
            (str(ctx.author.id),)
        ) as cursor:
            result = await cursor.fetchone()
            return result is not None

# Command check for premium users
async def is_premium(user_id: int) -> bool:
    """Check if a user has premium status"""
    # Convert to string to ensure compatibility with database
    user_id_str = str(user_id)
    
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            # Check if the table exists
            async with db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='premium_users'") as cursor:
                if not await cursor.fetchone():
                    # Table doesn't exist, create it
                    await db.execute("""
                        CREATE TABLE IF NOT EXISTS premium_users (
                            user_id TEXT PRIMARY KEY,
                            added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                            expires_at TIMESTAMP DEFAULT NULL
                        )
                    """)
                    await db.commit()
                    return False  # New table, user is not premium
            
            # Check if user is premium
            async with db.execute("SELECT 1 FROM premium_users WHERE user_id = ?", (user_id_str,)) as cursor:
                result = await cursor.fetchone()
                return result is not None
    except Exception as e:
        logger.error(f"Error checking premium status: {e}")
        return False  # Default to non-premium if there's an error

# ----- Safe interaction handling utilities -----

async def safe_respond(ctx, *args, **kwargs):
    """
    Safely respond to an interaction, handling common interaction errors.
    Returns True if response was successful, False otherwise.
    
    Usage: 
        success = await safe_respond(ctx, "Hello world!", ephemeral=True)
    """
    try:
        # First, try to check if defer is needed (e.g., for long operations)
        if not ctx.response.is_done() and kwargs.get('defer', False):
            try:
                # Remove the defer arg since it's not a valid ctx.respond parameter
                defer_ephemeral = kwargs.pop('defer_ephemeral', False)
                await ctx.defer(ephemeral=defer_ephemeral)
                
                # Brief pause to ensure defer is registered
                await asyncio.sleep(0.1)
            except Exception as e:
                # Log but continue - main response is more important
                logger.warning(f"Defer failed: {e}")
        
        # Now attempt to respond based on interaction state
        if not ctx.response.is_done():
            # Not responded yet, use standard respond
            await ctx.respond(*args, **kwargs)
            return True
        elif hasattr(ctx, 'followup'):
            # Already responded, use followup
            await ctx.followup.send(*args, **kwargs)
            return True
        else:
            # No available interaction methods
            logger.warning(f"Could not find a valid way to respond to interaction {ctx.id}")
            return False
    
    except discord.errors.NotFound as e:
        # Interaction token expired or message deleted
        if "10062" in str(e): # Unknown interaction
            logger.warning(f"Interaction token expired: {e}")
        else:
            logger.warning(f"NotFound error in safe_respond: {e}")
        return False
        
    except discord.errors.HTTPException as e:
        # Interaction already acknowledged
        if "40060" in str(e): # Interaction has already been acknowledged
            try:
                # Try using followup as a last resort
                await ctx.followup.send(*args, **kwargs)
                return True
            except Exception as inner_e:
                logger.warning(f"Failed to use followup after 40060 error: {inner_e}")
                return False
        else:
            logger.warning(f"HTTPException in safe_respond: {e}")
            return False
            
    except Exception as e:
        logger.warning(f"Error in safe_respond: {e}")
        return False

async def safe_defer(ctx, ephemeral=False):
    """
    Safely defer an interaction response, handling errors gracefully.
    
    Usage:
        await safe_defer(ctx, ephemeral=True)
    """
    try:
        if not ctx.response.is_done():
            await ctx.defer(ephemeral=ephemeral)
            return True
    except discord.errors.NotFound:
        # Unknown interaction, likely expired
        logger.warning(f"Could not defer interaction {ctx.id} - token may be expired")
        return False
    except discord.errors.HTTPException as e:
        if "40060" in str(e):  # Already acknowledged
            logger.warning(f"Interaction {ctx.id} already acknowledged, could not defer")
        else:
            logger.warning(f"HTTP error while deferring interaction {ctx.id}: {e}")
        return False
    except Exception as e:
        logger.warning(f"Error while deferring interaction {ctx.id}: {e}")
        return False
    return False  # Already responded case

async def safe_edit(message, *args, **kwargs):
    """
    Safely edit a message, handling common errors.
    Works with both interaction responses and regular messages.

    Usage:
        await safe_edit(message_or_interaction, content="Updated content", embed=new_embed)
    """
    try:
        if hasattr(message, 'edit'):
            # Regular message
            await message.edit(*args, **kwargs)
            return True
        elif hasattr(message, 'edit_original_response'):
            # Interaction
            await message.edit_original_response(*args, **kwargs)
            return True
        else:
            logger.warning("Object passed to safe_edit does not support editing")
            return False
    except discord.errors.NotFound:
        logger.warning(f"Message not found when trying to edit")
        return False
    except discord.errors.Forbidden:
        logger.warning(f"Missing permissions to edit message")
        return False
    except Exception as e:
        logger.warning(f"Error editing message: {e}")
        return False

class SafeContext:
    """
    Wrapper for Discord ApplicationContext to provide safer interaction handling.
    
    Usage:
        @bot.slash_command()
        async def my_command(ctx):
            safe_ctx = SafeContext(ctx)
            await safe_ctx.defer()
            # ... do long operation ...
            await safe_ctx.respond("Done!")
    """
    def __init__(self, ctx):
        self.ctx = ctx
        self._responded = False
        
    async def defer(self, ephemeral=False):
        """Safely defer response"""
        result = await safe_defer(self.ctx, ephemeral)
        return result
        
    async def respond(self, *args, **kwargs):
        """Safely respond to the interaction"""
        result = await safe_respond(self.ctx, *args, **kwargs)
        if result:
            self._responded = True
        return result
        
    async def followup(self, *args, **kwargs):
        """Safely send a followup message"""
        try:
            await self.ctx.followup.send(*args, **kwargs)
            return True
        except Exception as e:
            logger.warning(f"Error sending followup: {e}")
            return False
    
    @property
    def author(self):
        """Passthrough to original context"""
        return self.ctx.author
        
    @property
    def guild(self):
        """Passthrough to original context"""
        return self.ctx.guild
    
    @property
    def channel(self):
        """Passthrough to original context"""
        return self.ctx.channel
    
    def __getattr__(self, name):
        """Passthrough to original context for any other attributes"""
        return getattr(self.ctx, name)

# Define a decorator for adding safe handling to slash commands
def safe_command_handling(func):
    """
    Decorator for slash commands that adds safe interaction handling.
    
    Usage:
        @bot.slash_command()
        @safe_command_handling
        async def my_command(ctx, arg1, arg2):
            # Command implementation
    """
    @functools.wraps(func)
    async def wrapper(self, ctx, *args, **kwargs):
        # No longer auto-deferring here
        
        try:
            # Call the original function
            return await func(self, ctx, *args, **kwargs)
        except discord.errors.NotFound as e:
            if "10062" in str(e):  # Unknown interaction
                logger.warning(f"Interaction expired during command execution: {e}")
            else:
                logger.warning(f"NotFound error in command {func.__name__}: {e}")
        except discord.errors.HTTPException as e:
            if "40060" in str(e):  # Already acknowledged
                logger.warning(f"Interaction already acknowledged in command {func.__name__}")
            else:
                logger.error(f"HTTP error in command {func.__name__}: {e}")
                # Try to send a followup if possible
                try:
                    await ctx.followup.send("An error occurred while processing your command", ephemeral=True)
                except:
                    pass
        except Exception as e:
            logger.error(f"Error in command {func.__name__}: {e}")
            logger.error(traceback.format_exc())
            # Try to notify the user
            try:
                await ctx.followup.send("An unexpected error occurred", ephemeral=True)
            except:
                pass
    
    return wrapper

# Initialize the bot with intents
intents = discord.Intents.default()
intents.message_content = True

# Create bot instance with slash commands enabled by default
bot = commands.Bot(command_prefix="!", intents=intents)

# Function to set default attributes for all commands
def set_default_command_attributes():
    """Set default integration_types and contexts for all commands"""
    for command in bot.application_commands:
        command.integration_types = [IntegrationType.user_install]
        command.contexts = {
            InteractionContextType.guild, 
            InteractionContextType.bot_dm, 
            InteractionContextType.private_channel
        }
    logger.info("Default command attributes set")

# Event listener for when the bot is ready
@bot.event
async def on_ready():
    logger.info(f"Bot logged in as {bot.user.name}")
    
    # Set default attributes for all commands
    set_default_command_attributes()
    
    # Sync commands with Discord - this is required for py-cord
    logger.info("Syncing commands with Discord...")
    await bot.sync_commands()
    logger.info("Command sync complete")
    
    # Schedule regular command syncs to ensure commands remain registered
    asyncio.create_task(periodic_command_sync())

# Regular command sync to prevent "unknown integration" errors
async def periodic_command_sync():
    """Periodically sync commands to maintain registration"""
    while True:
        await asyncio.sleep(3600)  # Sync once per hour
        try:
            logger.info("Performing periodic command sync...")
            await bot.sync_commands()
            logger.info("Periodic command sync complete")
        except Exception as e:
            logger.error(f"Error during periodic command sync: {e}")

# Function to load extensions - keeps it simple and synchronous
def load_extensions():
    """Load all extensions from the commands directory"""
    commands_dir = "commands"
    
    loaded_count = 0
    failed_count = 0
    
    for filename in os.listdir(commands_dir):
        if filename.endswith(".py") and filename != "__init__.py":
            extension = f"{commands_dir}.{filename[:-3]}"
            try:
                bot.load_extension(extension)
                loaded_count += 1
            except Exception as e:
                logger.error(f"Failed to load extension {extension}: {e}")
                failed_count += 1
    
    logger.info(f"Extensions loaded: {loaded_count} success, {failed_count} failed")
    return loaded_count, failed_count

# Keep the main execution simple
if __name__ == "__main__":
    # Load extensions
    try:
        load_extensions()
    except Exception as e:
        logger.error(f"Error during extension loading: {e}")
    
    # Run the bot
    logger.info("Starting bot")
    bot.run(TOKEN)