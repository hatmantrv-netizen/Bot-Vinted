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
DB_NAME = "vinted_bot_v2.db"

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

if TOKEN is None:
    logging.error("❌ La variable d'environnement 'TOKEN' n'est pas définie dans ton système !")

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

# --- BOUTONS SOUS L'ARTICLE ---
class VintedItemView(ui.View):
    def __init__(self, item_url, negotiate_url):
        super().__init__(timeout=None)
        
        self.add_item(ui.Button(
            label="🔍 Voir détails", 
            style=discord.ButtonStyle.link, 
            url=item_url
        ))
        
        self.add_item(ui.Button(
            label="💬 Négocier", 
            style=discord.ButtonStyle.link, 
            url=negotiate_url
        ))

class VintedBot(discord.Client):
    # Number of scan cycles to skip a channel after a permission error before retrying.
    PERMISSION_SKIP_CYCLES = 10

    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)
        self.scraper = VintedScraper()
        self.db = None
        # Maps channel_id -> remaining cycles to skip due to a permission error.
        self._permission_errors: dict[int, int] = {}

    def _check_send_permissions(self, channel: discord.TextChannel) -> bool:
        """Return True if the bot has Send Messages + Embed Links in *channel*."""
        me = channel.guild.me
        perms = channel.permissions_for(me)
        if not perms.send_messages:
            logging.warning(
                f"⚠️  Permission manquante : 'Envoyer des messages' dans #{channel.name} "
                f"(id={channel.id}, serveur='{channel.guild.name}'). "
                "Ajoutez la permission ou retirez le filtre avec /vinted_clear."
            )
            return False
        if not perms.embed_links:
            logging.warning(
                f"⚠️  Permission manquante : 'Intégrer des liens' dans #{channel.name} "
                f"(id={channel.id}, serveur='{channel.guild.name}'). "
                "Ajoutez la permission ou retirez le filtre avec /vinted_clear."
            )
            return False
        return True


    async def setup_hook(self):
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
                # --- BACKOFF : skip channels that recently raised a 403 ---
                if channel_id in self._permission_errors:
                    remaining = self._permission_errors[channel_id] - 1
                    if remaining > 0:
                        self._permission_errors[channel_id] = remaining
                        logging.debug(
                            f"⏭️  Salon {channel_id} ignoré ({remaining} cycle(s) restant(s) "
                            "avant nouvelle tentative suite à une erreur de permission)."
                        )
                        continue
                    else:
                        del self._permission_errors[channel_id]
                        logging.info(
                            f"🔄 Nouvelle tentative pour le salon {channel_id} "
                            "après la période de cooldown."
                        )

                items = await self.scraper.fetch_items(url)
                channel = self.get_channel(channel_id)

                if not channel or not items:
                    continue

                # --- VÉRIFICATION DES PERMISSIONS AVANT ENVOI ---
                if not self._check_send_permissions(channel):
                    self._permission_errors[channel_id] = self.PERMISSION_SKIP_CYCLES
                    continue



                for item in items[:15]:
                    item_id = item.get('id')
                    if not item_id: continue
                    
                    # Vérification doublon
                    async with self.db.execute("SELECT 1 FROM seen_items WHERE item_id = ?", (item_id,)) as c:
                        if await c.fetchone():
                            continue

                    await self.db.execute("INSERT INTO seen_items (item_id, timestamp) VALUES (?, ?)", 
                                         (item_id, datetime.now()))
                    await self.db.commit()

                    # --- EXTRACTION SÉCURISÉE DES DONNÉES ---
                    title = item.get('title', 'Sans titre')
                    
                    # 1. Calcul du prix TTC (Frais Vinted = 0.70€ + 5% du prix)
                    price_info = item.get('price', {})
                    formatted_price = "0.00"
                    raw_currency = "EUR"
                    
                    if isinstance(price_info, dict):
                        formatted_price = price_info.get('amount', '0.00')
                        raw_currency = price_info.get('currency_code', 'EUR')
                    
                    try:
                        base_price = float(formatted_price)
                        # Formule officielle approximative des frais Vinted
                        frais_protection = 0.70 + (base_price * 0.05)
                        ttc_price = base_price + frais_protection
                        price_display = f"{base_price:.2f} {raw_currency} | ≈ {ttc_price:.2f} {raw_currency} (TTC)"
                    except (ValueError, TypeError):
                        price_display = f"{formatted_price} {raw_currency}"

                    # 2. Temps écoulé depuis la publication
                    published_at = "Récemment"
                    # Vinted fournit souvent la date sous forme de timestamp en secondes
                    created_ts = item.get('created_at_ts') or item.get('updated_at_ts')
                    if created_ts:
                        now_ts = datetime.now().timestamp()
                        diff_sec = now_ts - float(created_ts)
                        diff_min = int(diff_sec // 60)
                        
                        if diff_min < 1:
                            published_at = "À l'instant"
                        elif diff_min < 60:
                            published_at = f"il y a {diff_min} minute(s)"
                        else:
                            published_at = f"il y a {diff_min // 60} heure(s)"

                    # 3. Récupération des avis du vendeur
                    user_info = item.get('user', {})
                    feedback_count = user_info.get('feedback_count', 0)
                    # Vinted donne parfois une note sur 5 ou une réputation de 0 à 1
                    rating = user_info.get('rating') or 0
                    
                    # Génération visuelle des étoiles
                    stars_full = int(rating) if rating <= 5 else 0
                    stars_empty = 5 - stars_full
                    avis_display = f"{'⭐' * stars_full}{'☆' * stars_empty} ({feedback_count})" if feedback_count > 0 else "Pas d'avis"

                    # Autres informations classiques
                    brand = item.get('brand_title', 'Inconnue')
                    size = item.get('size_title', 'N/A')
                    condition = item.get('status_title') or item.get('status') or 'Non spécifié'
                    photo = item.get('photo', {}).get('url')
                    
                    item_url = f"https://www.vinted.fr/items/{item_id}"
                    negotiate_url = f"https://www.vinted.fr/messages/new?item_id={item_id}"

                    # --- CRÉATION DE L'EMBED (DESIGN GRILLE 3x2) ---
                    embed = discord.Embed(
                        title=title,
                        url=item_url,
                        color=0x09B0B0,
                        timestamp=datetime.now()
                    )
                    
                    # Ligne 1 : Publié / Marque / Taille
                    embed.add_field(name="⌛ Publié", value=published_at, inline=True)
                    embed.add_field(name="📕 Marque", value=brand, inline=True)
                    embed.add_field(name="📏 Taille", value=size, inline=True)
                    
                    # Ligne 2 : Avis / État / Prix
                    embed.add_field(name="💥 Avis", value=avis_display, inline=True)
                    embed.add_field(name="💎 État", value=condition, inline=True)
                    embed.add_field(name="💰 Prix", value=price_display, inline=True)
                    
                    if photo:
                        embed.set_image(url=photo)
                        
                    # Personnalisation du haut (Vendeur) et du bas
                    seller_name = user_info.get('login', 'Inconnu')
                    embed.set_author(name=f"Vendeur : {seller_name}")
                    embed.set_footer(text=f"Filtre actif : {name} • Vintra")

                    view = VintedItemView(item_url, negotiate_url)

                    try:
                        await channel.send(embed=embed, view=view)
                    except discord.Forbidden:
                        logging.error(
                            f"🚫 403 Forbidden dans #{channel.name} (id={channel.id}, "
                            f"serveur='{channel.guild.name}'): permissions manquantes. "
                            f"Ce salon sera ignoré pendant {self.PERMISSION_SKIP_CYCLES} "
                            "cycles. Corrigez les permissions ou supprimez le filtre avec /vinted_clear."
                        )
                        self._permission_errors[channel_id] = self.PERMISSION_SKIP_CYCLES
                        break  # stop processing items for this channel
                    except discord.HTTPException as e:
                        logging.error(
                            f"⚠️  Erreur HTTP Discord dans #{channel.name} "
                            f"(status={e.status}, code={e.code}): {e.text}"
                        )

                    await asyncio.sleep(0.5)

        except Exception as e:
            logging.error(f"Erreur critique boucle de scan : {e}")

# --- COMMANDES SLASH ---
bot = VintedBot()

@bot.tree.command(name="vinted_add", description="Ajouter une recherche Vinted à surveiller")
async def add_filter(interaction: discord.Interaction, nom_du_filtre: str, url_vinted: str):
    if "vinted.fr" not in url_vinted:
        await interaction.response.send_message("❌ URL Vinted invalide.", ephemeral=True)
        return
    
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

@bot.tree.command(name="vinted_check_permissions", description="Vérifier les permissions du bot dans ce salon")
async def check_permissions(interaction: discord.Interaction):
    channel = interaction.channel
    me = channel.guild.me
    perms = channel.permissions_for(me)

    missing = []
    if not perms.send_messages:
        missing.append("❌ **Envoyer des messages** — requis pour poster les annonces")
    if not perms.embed_links:
        missing.append("❌ **Intégrer des liens** — requis pour afficher les embeds")
    if not perms.attach_files:
        missing.append("⚠️  **Joindre des fichiers** — recommandé")
    if not perms.read_message_history:
        missing.append("⚠️  **Voir l'historique** — recommandé")

    if not missing:
        await interaction.response.send_message(
            f"✅ Le bot dispose de toutes les permissions nécessaires dans **#{channel.name}**.",
            ephemeral=True
        )
    else:
        lines = "\n".join(missing)
        await interaction.response.send_message(
            f"⚠️  Permissions manquantes dans **#{channel.name}** :\n{lines}\n\n"
            "Corrigez ces permissions dans les paramètres du salon, puis relancez cette commande pour vérifier.",
            ephemeral=True
        )


if __name__ == "__main__":
    if TOKEN:
        bot.run(TOKEN)
    else:
        logging.critical("Impossible de lancer le bot sans Token.")