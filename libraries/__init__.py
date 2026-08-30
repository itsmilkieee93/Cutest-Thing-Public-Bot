# libraries/__init__.py

__version__ = "9.1.0"
__bot_name__ = "Cutest Thing 🌸✨️"
__discord__ = "10"
__description__ = "Floating on a strawberry cloud 🍓☁️"

# ─────────────────────────────────────────────────────────────────────────────
# Custom module imports — imported here so bot_service.py can pull everything
# from a single `from libraries import (...)` statement.
# ─────────────────────────────────────────────────────────────────────────────
import wikipedia
import bot_info
import news
import news_api
import utils_module       as utilities
import music
import forward_msg         as forward
import weather
import youtube             as yt
import wifi
import encryption          as enc
import music_downloader    as downloader
import embed_msg           as embed
import calculator
import chatting_fun
import my_youtube_channel  as channel
import unsplash
from resources import shared
import commands_ai
import cloudflare_ai       as cake
import commands_fun
import commands_messaging
import commands_utility
import pexels
import openrouter
import summarize           as brief
import permissions as perms
import random_msg
import server_info
import server_persona as cute
import photo_editor as gen
import status_commands as status_bot