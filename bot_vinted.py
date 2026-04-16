import asyncio
import aiohttp
import aiosqlite
import discord
from discord import app_commands, ui
from discord.ext import tasks
import logging
from datetime import datetime
import os

# --- CONFIGURATION ---
TOKEN = os.getenv("TOKEN")
CHECK_INTERVAL = 30  # Secondes entre chaque scan
DB_NAME = "vinted_bot_v2.db" # Nouvelle DB pour être propre

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# Sécurité si le token n'est pas trouvé
if TOKEN is None:
    logging.error("❌ La variable d'environnement 'TOKEN' n'est pas définie dans Railway ou dans ton système !")

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
            self.session = aiohttp.ClientSession(headers=self.headers)
        return self.session

    async def fetch_cookies(self):
        """Initialise la session et récupère les cookies nécessaires"""
        try:
            session = await self._get_session()
            async with session.get("https://www.vinted.fr/") as resp:
                self.cookies = resp.cookies
                logging.info("Initialisation réussie : Nouveaux cookies Vinted récupérés.")
        except Exception as e:
            logging.error(f"Erreur d'initialisation (cookies): {e}")

    async def fetch_items(self, url):
        """Récupère les items en convertissant l'URL en appel API"""
        if self.cookies is None:
            await self.fetch_cookies()

        session = await self._get_session()
        
        # Transformation robuste de l'URL
        if "api/v2/catalog/items" not in url:
            api_url = url.replace("vinted.fr/catalog", "vinted.fr/api/v2/catalog/items")
        else:
            api_url = url

        try:
            async with session.get(api_url, cookies=self.cookies) as resp:
                if resp.status in [401, 403]:
                    logging.warning(f"Accès refusé ({resp.status}), rafraîchissement des cookies...")
                    await self.fetch_cookies()
                    return []
                
                if resp.status == 200:
                    data = await resp.json()
                    return data.get('items', [])
                
                logging.error(f"Vinted API Error: Status {resp.status}")
                return []
        except Exception as e:
            logging.error(f"Erreur réseau lors de la requête : {e}")
            return []

# --- CLASSE POUR LES BOUTONS ---
class VintedItemView(ui.View):
    def __init__(self, item_url, negotiate_url):
        super().__init__(timeout=None) # Boutons permanents
        
        # Bouton Link: "Voir détails"
        self.add_item(ui.Button(
            label="🔍 Voir détails", 
            style=discord.ButtonStyle.link, 
            url=item_url
        ))
        
        # Bouton Link: "Négocier"
        self.add_item(ui.Button(
            label="💬 Négocier", 
            style=discord.ButtonStyle.link, 
            url=negotiate_url
        ))

class VintedBot(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)
        self.scraper = VintedScraper()
        self.db = None

    async def setup_hook(self):
        # Initialisation Base de données
        self.db = await aiosqlite.connect(DB_NAME)
        await self.db.execute("""
            CREATE TABLE IF NOT EXISTS filters (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                channel_id INTEGER,
                user_id INTEGER,
                url TEXT,
                name TEXT
            )
        """)
        await self.db.execute("""
            CREATE TABLE IF NOT EXISTS seen_items (
                item_id INTEGER PRIMARY KEY,
                timestamp DATETIME
            )
        """)
        await self.db.commit()
        # Lancement de la boucle de scan
        self.scan_vinted.start()

    async def on_ready(self):
        await self.tree.sync()
        logging.info(f"✅ Bot connecté : {self.user}")
        await self.scraper.fetch_cookies()

    @tasks.loop(seconds=CHECK_INTERVAL)
    async def scan_vinted(self):
        try:
            async with self.db.execute("SELECT channel_id, url, name FROM filters") as cursor:
                filters = await cursor.fetchall()

            for channel_id, url, name in filters:
                items = await self.scraper.fetch_items(url)
                channel = self.get_channel(channel_id)
                
                if not channel or not items:
                    continue

                for item in items[:15]:  # On analyse les 15 derniers
                    item_id = item.get('id')
                    if not item_id: continue
                    
                    # Vérification doublon
                    async with self.db.execute("SELECT 1 FROM seen_items WHERE item_id = ?", (item_id,)) as c:
                        if await c.fetchone():
                            continue

                    # Ajout en base
                    await self.db.execute("INSERT INTO seen_items (item_id, timestamp) VALUES (?, ?)", 
                                         (item_id, datetime.now()))
                    await self.db.commit()

                    # --- EXTRACTION SÉCURISÉE DES DONNÉES ---
                    title = item.get('title', 'Sans titre')
                    
                    # Correction et sécurité absolue du prix
                    price_info = item.get('price')
                    formatted_price = "??"
                    raw_currency = "€"
                    
                    if isinstance(price_info, dict):
                        formatted_price = price_info.get('amount', '??')
                        raw_currency = price_info.get('currency_code', '€')
                        try:
                            formatted_price = f"{float(formatted_price):.2f}"
                        except (ValueError, TypeError):
                            pass
                    elif isinstance(price_info, (str, float, int)):
                        formatted_price = str(price_info)
                        raw_currency = item.get('currency', '€')
                    
                    brand = item.get('brand_title', 'Inconnue')
                    size = item.get('size_title', 'N/A')
                    
                    # Correction de l'état (On vérifie plusieurs clés pour être sûr)
                    condition = item.get('status_title') or item.get('status') or 'Non spécifié'
                    
                    photo = item.get('photo', {}).get('url')
                    item_url = f"https://www.vinted.fr/items/{item_id}"
                    
                    # URL de négociation
                    negotiate_url = f"https://www.vinted.fr/messages/new?item_id={item_id}"

                    # --- ENVOI DE L'EMBED ---
                    embed = discord.Embed(
                        title=title,
                        url=item_url,
                        color=0x09B0B0, # Couleur Vinted turquoise
                        timestamp=datetime.now()
                    )
                    
                    embed.add_field(name="💰 Prix", value=f"**{formatted_price} {raw_currency}**", inline=True)
                    embed.add_field(name="📏 Taille", value=size, inline=True)
                    embed.add_field(name="🏷️ Marque", value=brand, inline=True)
                    embed.add_field(name="✨ État", value=condition, inline=True)
                    
                    if photo:
                        embed.set_image(url=photo)
                    embed.set_footer(text=f"Filtre activé : {name}")

                    view = VintedItemView(item_url, negotiate_url)

                    try:
                        await channel.send(embed=embed, view=view)
                    except Exception as e:
                        logging.error(f"Erreur Discord API: {e}")
                    
                    await asyncio.sleep(0.5) # Petit délai anti-spam

        except Exception as e:
            logging.error(f"Erreur critique boucle de scan : {e}")

# --- COMMANDES SLASH ---
bot = VintedBot()

@bot.tree.command(name="vinted_add", description="Ajouter une recherche Vinted à surveiller")
async def add_filter(interaction: discord.Interaction, nom_du_filtre: str, url_vinted: str):
    if "vinted.fr" not in url_vinted:
        await interaction.response.send_message("❌ URL Vinted invalide.", ephemeral=True)
        return
    
    # On force le tri par nouveautés
    if "order=newest_first" not in url_vinted:
        separator = "&" if "?" in url_vinted else "?"
        url_vinted += f"{separator}order=newest_first"

    await bot.db.execute("INSERT INTO filters (channel_id, user_id, url, name) VALUES (?, ?, ?, ?)",
                        (interaction.channel_id, interaction.user.id, url_vinted, nom_du_filtre))
    await bot.db.commit()
    await interaction.response.send_message(f"✅ Filtre '**{nom_du_filtre}**' activé dans ce salon !", ephemeral=False)

@bot.tree.command(name="vinted_clear", description="Supprimer tous les filtres de ce salon")
async def clear_filters(interaction: discord.Interaction):
    if not interaction.user.guild_permissions.manage_channels:
        await interaction.response.send_message("❌ Tu dois avoir la permission de gérer les salons.", ephemeral=True)
        return

    await bot.db.execute("DELETE FROM filters WHERE channel_id = ?", (interaction.channel_id,))
    await bot.db.commit()
    await interaction.response.send_message("🗑️ Tous les filtres de ce salon ont été supprimés.")

if __name__ == "__main__":
    if TOKEN:
        bot.run(TOKEN)
    else:
        logging.critical("Impossible de lancer le bot sans Token.")