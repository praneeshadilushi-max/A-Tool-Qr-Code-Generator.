import telebot
from database import get_all_history

TOKEN = "8263513374:AAFoF395YXL6AOW8eqdofPtR7sWUPTJbnik"  # Telegram bot token
bot = telebot.TeleBot(TOKEN)

ADMIN_ID = 7874548648# Telegram admin ID

@bot.message_handler(commands=['history'])
def send_history(message):
    if message.from_user.id != ADMIN_ID:
        bot.reply_to(message, "ඔයා admin නොවේ.")
        return
    
    all_users = get_all_history()
    response = ""
    for item in all_users:
        response += f"{item['username']} - {item['qr_data']} - {item['time']}\n"
    
    if response == "":
        response = "History එකේ data එකක් නැහැ."
    
    bot.send_message(message.chat.id, response)

bot.polling()