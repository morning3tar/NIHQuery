import logging
import os
import requests
import json
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.constants import ParseMode, ChatAction
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
    CallbackQueryHandler,
)
from typing import List, Dict, Any
from thefuzz import fuzz

# Configuration
load_dotenv()
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
NIH_API_URL = "https://api.reporter.nih.gov/v2/projects/search"
ITEMS_PER_PAGE = 5
FUZZY_MATCH_THRESHOLD = 80

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)


def parse_name(full_name: str) -> tuple[str | None, str | None]:
    parts = full_name.strip().split()
    if len(parts) >= 2:
        first_name = parts[0]
        last_name = " ".join(parts[1:])
        return first_name, last_name
    else:
        logger.warning(f"Could not parse name: '{full_name}'")
        return None, None

async def fetch_nih_data(first_name: str, last_name: str) -> list | None:
    payload = {
        "criteria": {
            "pi_names": [
                {
                    "first_name": first_name,
                    "last_name": last_name
                }
            ]
        },
        "offset": 0,
        "limit": 75
    }
    headers = {'Content-Type': 'application/json', 'accept': 'application/json'}
    logger.info(f"Querying NIH API for '{first_name} {last_name}'")

    try:
        response = requests.post(NIH_API_URL, headers=headers, json=payload, timeout=30)
        response.raise_for_status()
        content_type = response.headers.get('Content-Type', '').lower()
        if 'application/json' in content_type:
            try:
                data = response.json()
                results = data.get("results")
                if results is None: return []
                if not isinstance(results, list): return []
                logger.info(f"API returned {len(results)} results for '{first_name} {last_name}'.")
                return results
            except json.JSONDecodeError as e:
                logger.error(f"FAILED to decode API JSON response: {e}")
                return None
        else:
            logger.error(f"NIH API returned non-JSON Content-Type: {content_type}")
            return None
    except requests.exceptions.Timeout:
        logger.error("NIH API request timed out.")
        return None
    except requests.exceptions.HTTPError as http_err:
        logger.error(f"HTTP error occurred during NIH API call: {http_err}")
        return None
    except requests.exceptions.RequestException as e:
        logger.error(f"NIH API request failed (Network/Connection Error): {e}")
        return None
    except Exception as e:
        logger.error(f"An unexpected error occurred during API fetch: {e}", exc_info=True)
        return None


def format_pi_list(pi_list: List[Dict[str, Any]]) -> str:
    if not pi_list: return "N/A"
    formatted_pis = []
    contact_pi = None
    other_pis = []
    for pi in pi_list:
        name = pi.get('full_name', '').strip()
        if not name:
             first = pi.get('first_name', '')
             last = pi.get('last_name', '')
             name = f"{first} {last}".strip()
        if pi.get('is_contact_pi'):
            contact_pi = f"*{name}*"
        elif name:
             other_pis.append(name)
    if contact_pi: formatted_pis.append(contact_pi)
    formatted_pis.extend(sorted(other_pis))
    return "; ".join(formatted_pis) if formatted_pis else "N/A"

def format_results_page(results: list, page: int, items_per_page: int) -> str:
    start_index = page * items_per_page
    end_index = start_index + items_per_page
    page_results = results[start_index:end_index]
    response_text = ""
    for project in page_results:
        if not isinstance(project, dict): continue

        title = project.get('project_title', 'N/A')
        appl_id = project.get('appl_id', 'N/A')
        year = project.get('fiscal_year', 'N/A')
        total_amount = project.get('award_amount')
        org_data = project.get('organization', {})
        org = org_data.get('org_name', 'N/A') if isinstance(org_data, dict) else 'N/A'
        pi_list_data = project.get('principal_investigators', [])
        pi_string = format_pi_list(pi_list_data)
        direct_cost = project.get('direct_cost_amt')
        indirect_cost = project.get('indirect_cost_amt')

        total_amount_str = f"${total_amount:,.0f}" if isinstance(total_amount, (int, float)) else "N/A"
        direct_cost_str = f"${direct_cost:,.0f}" if isinstance(direct_cost, (int, float)) else "N/A"
        indirect_cost_str = f"${indirect_cost:,.0f}" if isinstance(indirect_cost, (int, float)) else "N/A"

        title_escaped = str(title).replace('*', '\\*').replace('_', '\\_').replace('[','\\[').replace(']','\\]')

        project_info = (
            f"\n- *Title:* {title_escaped}\n"
            f"- *Appl ID:* {appl_id}\n"
            f"- *PIs:* {pi_string}\n"
            f"- *Org:* {org}\n"
            f"- *Total Amount:* {total_amount_str}\n"
            f"- *Direct Cost:* {direct_cost_str}\n"
            f"- *Indirect Cost:* {indirect_cost_str}\n"
            f"- *Year:* {year}\n"
            f"----------------------------------"
        )
        response_text += project_info

    if not response_text:
        return "\n_(No projects found on this page.)_"
    return response_text

def create_pagination_keyboard(total_items: int, items_per_page: int, current_page: int) -> InlineKeyboardMarkup | None:
    if total_items <= items_per_page: return None
    total_pages = (total_items + items_per_page - 1) // items_per_page
    keyboard = []
    row = []
    if current_page > 0:
        row.append(InlineKeyboardButton("⬅️ Previous", callback_data=f"paginate_{current_page - 1}"))
    else:
        row.append(InlineKeyboardButton(" ", callback_data="no_op"))
    row.append(InlineKeyboardButton(f"Page {current_page + 1}/{total_pages}", callback_data="no_op"))
    if current_page < total_pages - 1:
        row.append(InlineKeyboardButton("Next ➡️", callback_data=f"paginate_{current_page + 1}"))
    else:
        row.append(InlineKeyboardButton(" ", callback_data="no_op"))
    keyboard.append(row)
    return InlineKeyboardMarkup(keyboard)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_name = update.effective_user.first_name
    welcome_message = (
        f"Hi {user_name}! 👋 Welcome to the NIH Funding Bot!\n\n"
        "I can help you find NIH grant information for Principal Investigators.\n\n"
        "➡️ Just send me the full name of a PI (e.g., Andrew Ng).\n\n"
        "*Available Commands:*\n"
        "/help - Show the information on how to use the bot."
        "\n\n*Disclaimer:*\n"
        "_This bot is not affiliated with the NIH. The information provided is based on publicly available data from the NIH RePORTER database._"
        "\n\n*Privacy Note:*\n"
        "_I do not store any personal data or conversation history. Your queries are processed in real-time and not saved._"
        "\n\n*Feedback:*\n_If you have any suggestions or encounter issues, please reach out to the bot developer:_ @xxxx\\_Bot"
    )
    context.user_data.pop('results', None)
    context.user_data.pop('page', None)
    context.user_data.pop('pi_query', None)
    await update.message.reply_text(welcome_message, parse_mode=ParseMode.MARKDOWN)

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    help_text = (
        "ℹ️ *How to Use This Bot*\n\n"
        "1️⃣ Simply type the *full name* of the Principal Investigator you're interested in (e.g., `Andrew Ng`).\n\n"
        "2️⃣ I will search the NIH RePORTER database and show you their funded projects, including:\n\n"
        "   - Project Title\n"
        "   - Application ID\n"
        "   - Principal Investigators (Contact PI is bolded)\n"
        "   - Organization Name\n"
        "   - Total Amount\n"
        "   - Direct Cost\n"
        "   - Indirect Cost\n"
        "   - Fiscal Year\n\n"
        "Navigation Use the ⬅️➡️ buttons to navigate if there are multiple pages of results.\n\n"
        "❓ *Note:* I perform a basic check for name similarity. If the results seem off, please double check the spelling!\n\n"
        "*Available Commands:*\n"
        "/start - Show the welcome message & clear search.\n"
        "/help - Show this help message."
        "\n\n*Disclaimer:*\n"
        "_This bot is not affiliated with the NIH. The information provided is based on publicly available data from the NIH RePORTER database._"
        "\n\n*Privacy Note:*\n"
        "_I do not store any personal data or conversation history. Your queries are processed in real-time and not saved._"
        "\n\n*Feedback:*\n_If you have any suggestions or encounter issues, please reach out to the bot developer:_ @xxxx\\_Bot"
        
    )
    await update.message.reply_text(help_text, parse_mode=ParseMode.MARKDOWN)   

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:

    user_input_name = update.message.text
    logger.info(f"Received query from user {update.effective_user.id}: '{user_input_name}'")

    context.user_data.pop('results', None)
    context.user_data.pop('page', None)
    context.user_data.pop('pi_query', None)

    first_name, last_name = parse_name(user_input_name)
    if not first_name or not last_name:
        await update.message.reply_text(
            "Hmm, please enter the *full name* like 'First Last' (e.g., 'Andrew Ng').",
            parse_mode=ParseMode.MARKDOWN
        )
        return

    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)
    results = await fetch_nih_data(first_name, last_name)

    if results is None:
        logger.error("fetch_nih_data returned None.")
        await update.message.reply_text("I couldn't connect to the NIH database right now. Please try again in a moment.")
        return

    if not results:
        logger.info(f"No results found for '{first_name} {last_name}'.")
        await update.message.reply_text(
            f"⚠️ Couldn't find NIH funding records for *{first_name} {last_name}*. Please double check the spelling and try again.",
            parse_mode=ParseMode.MARKDOWN
        )
        return

    context.user_data['results'] = results
    context.user_data['page'] = 0
    context.user_data['pi_query'] = user_input_name
    current_page = 0
    total_items = len(results)

    fuzzy_note = ""
    try:
        first_result_pis = results[0].get('principal_investigators', [])
        primary_pi_name = ""
        if first_result_pis:
            contact_pi_found = False
            for pi in first_result_pis:
                if pi.get('is_contact_pi'):
                    primary_pi_name = pi.get('full_name', f"{pi.get('first_name','')} {pi.get('last_name','')}").strip()
                    contact_pi_found = True
                    break
            if not contact_pi_found and first_result_pis[0]:
                 pi = first_result_pis[0]
                 primary_pi_name = pi.get('full_name', f"{pi.get('first_name','')} {pi.get('last_name','')}").strip()

        if primary_pi_name:
            score = fuzz.token_sort_ratio(user_input_name.lower(), primary_pi_name.lower())
            logger.info(f"Fuzzy match score between '{user_input_name}' and primary PI '{primary_pi_name}': {score}")
            if score < FUZZY_MATCH_THRESHOLD:
                 fuzzy_note = f"\n\n_Showing results for projects linked to {primary_pi_name} (Similarity score: {score})_"
        else:
             logger.warning("Could not determine primary PI name from first result for fuzzy check.")
    except Exception as e:
        logger.error(f"Error during fuzzy matching check: {e}")

    total_pages = (total_items + ITEMS_PER_PAGE - 1) // ITEMS_PER_PAGE
    page_text = format_results_page(results, current_page, ITEMS_PER_PAGE)
    keyboard = create_pagination_keyboard(total_items, ITEMS_PER_PAGE, current_page)
    message_text = f"✅ Found {total_items} project(s) for query *{user_input_name}* (Page {current_page + 1}/{total_pages}):{fuzzy_note}\n" + page_text

    if len(message_text) > 4096:
        message_text = message_text[:4090] + "\n_[Message truncated]_"
        logger.warning(f"First page message truncated for query '{user_input_name}'")

    await update.message.reply_text(
        text=message_text,
        reply_markup=keyboard,
        parse_mode=ParseMode.MARKDOWN
    )

async def pagination_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    callback_data = query.data
    logger.debug(f"Received callback query: {callback_data}")

    if callback_data == "no_op": return

    try:
        action, page_str = callback_data.split('_')
        if action != 'paginate': return
        target_page = int(page_str)
    except (ValueError, IndexError):
        logger.error(f"Could not parse callback_data: {callback_data}")
        return

    results = context.user_data.get('results')
    pi_query = context.user_data.get('pi_query', 'this PI')

    if results is None:
        await query.edit_message_text(text="Sorry, these results seem to have expired. Please perform the search again.", reply_markup=None)
        return

    total_items = len(results)
    current_page = target_page
    total_pages = (total_items + ITEMS_PER_PAGE - 1) // ITEMS_PER_PAGE

    if not 0 <= current_page < total_pages:
         logger.warning(f"Invalid target page requested: {current_page}. Total pages: {total_pages}")
         return

    context.user_data['page'] = current_page
    page_text = format_results_page(results, current_page, ITEMS_PER_PAGE)
    keyboard = create_pagination_keyboard(total_items, ITEMS_PER_PAGE, current_page)
    message_text = f"✅ Found {total_items} project(s) for query *{pi_query}* (Page {current_page + 1}/{total_pages}):\n" + page_text

    if len(message_text) > 4096:
        message_text = message_text[:4090] + "\n_[Message truncated]_"
        logger.warning(f"Paginated message truncated for query '{pi_query}' on page {current_page + 1}")

    try:
        await query.edit_message_text(
            text=message_text,
            reply_markup=keyboard,
            parse_mode=ParseMode.MARKDOWN
        )
    except Exception as e:
        if "Message is not modified" in str(e):
            logger.debug("Message not modified, likely duplicate button press.")
        else:
            logger.error(f"Failed to edit message: {e}")
            await context.bot.send_message(chat_id=query.message.chat_id, text="⚠️ Error updating the view. Please try the search again if needed.")

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Exception while handling an update:", exc_info=context.error)

def main() -> None:
    if not TELEGRAM_BOT_TOKEN:
        logger.critical("FATAL ERROR: TELEGRAM_BOT_TOKEN environment variable not set.")
        return

    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    application.add_handler(CallbackQueryHandler(pagination_handler, pattern='^paginate_'))
    application.add_handler(CallbackQueryHandler(lambda u,c: u.callback_query.answer(), pattern='^no_op$'))
    application.add_error_handler(error_handler)

    logger.info("Starting NIH Funding Bot polling...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)
    logger.info("NIH Funding Bot stopped.")

if __name__ == "__main__":
    main()
