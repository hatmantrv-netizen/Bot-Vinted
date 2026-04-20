import asyncio
import aiohttp
import aiosqlite
import discord
from discord import app_commands, ui
from discord.ext import tasks
import logging
from datetime import datetime
import os

# --- INITIALISATION & CONFIGURATION ---
TOKEN = os.getenv("TOKEN")
CHECK_INTERVAL = 30  
DB_NAME = "vinted_bot_v2.db"

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

if TOKEN is None:
    logging.error("❌ TOKEN introuvable ! Vérifie ton fichier .env ou tes variables Railway.")

class VintedScraper:
    def __init__(self):
        self.session = None
        self.headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "fr-FR,fr;q=0.9",
            "Referer": "https://www.vinted.fr/"
        }
        self.cookies = None

    async def _get_session(self):
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession(headers=self.headers, timeout=aiohttp.ClientTimeout(total=15))
        return self.session

    async def fetch_cookies(self):
        try:
            session = await self._get_session()
            async with session.get("https://www.vinted.fr/") as resp:
                self.cookies = resp.cookies
                logging.info("🍪 Cookies Vinted actualisés.")
        except Exception as e:
            logging.error(f"Erreur cookies : {e}")

    async def fetch_items(self, url):
        if self.cookies is None: await self.fetch_cookies()
        session = await self._get_session()
        
        api_url = url.replace("vinted.fr/catalog", "vinted.fr/api/v2/catalog/items") if "api/v2" not in url else url

        try:
            async with session.get(api_url, cookies=self.cookies) as resp:
                if resp.status in [401, 403]:
                    await self.fetch_cookies()
                    return []
                if resp.status == 200:
                    data = await resp.json()
                    return data.get('items', [])
                return []
        except Exception:
            return []

# --- INTERFACE (BOUTONS) ---
class VintedItemView(ui.View):
    def __init__(self, item_url, negotiate_url):
        super().__init__(timeout=None)
        self.add_item(ui.Button(label="🔍 Voir détails", style=discord.ButtonStyle.link, url=item_url))
        self.add_item(ui.Button(label="💬 Négocier", style=discord.ButtonStyle.link, url=negotiate_url))

class VintedBot(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)
        self.scraper = VintedScraper()
        self.db = None

    async def setup_hook(self):
        self.db = await aiosqlite.connect(DB_NAME)
        await self.db.execute("CREATE TABLE IF NOT EXISTS filters (id INTEGER PRIMARY KEY, channel_id INTEGER, user_id INTEGER, url TEXT, name TEXT)")
        await self.db.execute("CREATE TABLE IF NOT EXISTS seen_items (item_id INTEGER PRIMARY KEY, timestamp DATETIME)")
        await self.db.commit()
        self.scan_vinted.start()

    async def on_ready(self):
        await self.tree.sync()
        logging.info(f"✅ Bot prêt : {self.user}")
        await self.scraper.fetch_cookies()

    @tasks.loop(seconds=CHECK_INTERVAL)
    async def scan_vinted(self):
        try:
            async with self.db.execute("SELECT channel_id, url, name FROM filters") as cursor:
                filters = await cursor.fetchall()

            for channel_id, url, name in filters:
                items = await self.scraper.fetch_items(url)
                channel = self.get_channel(channel_id)
                if not channel or not items: continue

                for item in items[:15]:
                    item_id = item.get('id')
                    async with self.db.execute("SELECT 1 FROM seen_items WHERE item_id = ?", (item_id,)) as c:
                        if await c.fetchone(): continue

                    await self.db.execute("INSERT INTO seen_items (item_id, timestamp) VALUES (?, ?)", (item_id, datetime.now()))
                    await self.db.commit()

                    # --- EXTRACTION & CALCULS ---
                    # 1. Temps écoulé
                    published_at = "À l'instant"
                    ts = item.get('created_at_ts') or item.get('updated_at_ts')
                    if ts:
                        diff = int(datetime.now().timestamp() - float(ts))
                        if diff < 60: published_at = "À l'instant"
                        elif diff < 3600: published_at = f"il y a {diff // 60} min"
                        else: published_at = f"il y a {diff // 3600} h"

                    # 2. Avis Vendeur
                    user = item.get('user', {})
                    rating = user.get('rating') or 0
                    f_count = user.get('feedback_count', 0)
                    stars = "⭐" * int(round(float(rating))) if f_count > 0 else "Nouveau"
                    avis_txt = f"{stars} ({f_count})"

                    # 3. Prix & TTC (0.70€ + 5%)
                    price_val = item.get('price', {}).get('amount', '0.00')
                    currency = item.get('price', {}).get('currency_code', '€')
                    try:
                        p = float(price_val)
                        ttc = p + 0.70 + (p * 0.05)
                        price_txt = f"**{p:.2f} {currency}**\n*(TTC: {ttc:.2f} {currency})*"
                    except: price_txt = f"**{price_val} {currency}**"

                    # --- CONSTRUCTION EMBED ---
                    embed = discord.Embed(title=item.get('title'), url=f"https://www.vinted.fr/items/{item_id}", color=0x09B0B0, timestamp=datetime.now())
                    embed.add_field(name="⌛ Publié", value=published_at, inline=True)
                    embed.add_field(name="📕 Marque", value=item.get('brand_title', 'N/A'), inline=True)
                    embed.add_field(name="📏 Taille", value=item.get('size_title', 'N/A'), inline=True)
                    embed.add_field(name="💥 Avis", value=avis_txt, inline=True)
                    embed.add_field(name="💎 État", value=item.get('status_title', 'N/A'), inline=True)
                    embed.add_field(name="💰 Prix", value=price_txt, inline=True)
                    
                    if item.get('photo', {}).get('url'):
                        embed.set_image(url=item['photo']['url'])
                    
                    embed.set_author(name=f"Vendeur : {user.get('login', 'Inconnu')}")
                    embed.set_footer(text=f"Filtre : {name}") # "Vintra" supprimé ici

                    view = VintedItemView(f"https://www.vinted.fr/items/{item_id}", f"https://www.vinted.fr/messages/new?item_id={item_id}")
                    await channel.send(embed=embed, view=view)
                    await asyncio.sleep(0.5)

        except Exception as e:
            logging.error(f"Erreur boucle : {e}")

# --- COMMANDES ---
bot = VintedBot()

@bot.tree.command(name="vinted_add", description="Ajouter un filtre")
async def add_filter(interaction: discord.Interaction, nom_du_filtre: str, url_vinted: str):
    if "vinted.fr" not in url_vinted:
        return await interaction.response.send_message("❌ URL invalide", ephemeral=True)
    if "order=newest_first" not in url_vinted:
        url_vinted += ("&" if "?" in url_vinted else "?") + "order=newest_first"
    
    await bot.db.execute("INSERT INTO filters (channel_id, user_id, url, name) VALUES (?, ?, ?, ?)", (interaction.channel_id, interaction.user.id, url_vinted, nom_du_filtre))
    await bot.db.commit()
    await interaction.response.send_message(f"✅ Filtre '**{nom_du_filtre}**' ajouté !")

@bot.tree.command(name="vinted_clear", description="Vider le salon")
async def clear_filters(interaction: discord.Interaction):
    await bot.db.execute("DELETE FROM filters WHERE channel_id = ?", (interaction.channel_id,))
    await bot.db.commit()
    await interaction.response.send_message("🗑️ Salon nettoyé.")

if __name__ == "__main__":
    bot.run(TOKEN)
