import os
import pika
import threading
import json
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from telegram import Update, Bot
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    CallbackContext,
)
import uvicorn
from threading import Lock

# Configuration
BOT_TOKEN = os.getenv("BOT_TOKEN")
RABBITMQ_HOST = os.getenv("RABBITMQ_HOST")
RABBITMQ_USER = os.getenv("RABBITMQ_USER")
RABBITMQ_PASSWORD = os.getenv("RABBITMQ_PASSWORD")
QUEUE_NAME = "message_queue"
API_PORT = 5555

print(f"Connecting to RabbitMQ with host={RABBITMQ_HOST}, user={RABBITMQ_USER}")

# Initialize FastAPI
app = FastAPI(
    title="Telegram Bot and RabbitMQ Integration API",
    description="An API for handling Telegram bot messages with RabbitMQ integration. Users can send messages, which are queued, processed, and responded to.",
    version="1.0.0",
)

class ProcessMessageRequest(BaseModel):
    message_id: int
    reply_text: str
    user_id: int


pending_users = set()
lock = Lock()

# RabbitMQ Connection
def get_rabbitmq_connection():
    credentials = pika.PlainCredentials(RABBITMQ_USER, RABBITMQ_PASSWORD)
    return pika.BlockingConnection(pika.ConnectionParameters(host=RABBITMQ_HOST, credentials=credentials))

# Define /start command handler
async def start(update: Update, context: CallbackContext):
    if update.message:
        await update.message.reply_text(
            "Добро пожаловать! 👋 \n\nЗадавайте вопросы о технике Amazone, и я постараюсь вам помочь.\n\n"
            "Можно 🔍 искать информацию командой /search или собирать чеклист 📋 командой /checklist"
        )

# Dictionary to track user modes (default is "checklist")
user_modes = {}
default_mode = "checklist"

# Define /search command handler
async def search(update: Update, context: CallbackContext):
    """
    Set the mode to "search" for the user.
    """
    user_id = update.message.chat.id
    user_modes[user_id] = "search"
    await update.message.reply_text("Теперь я буду искать информацию 🔍")

# Define /checklist command handler
async def checklist(update: Update, context: CallbackContext):
    """
    Set the mode to "checklist" for the user.
    """
    user_id = update.message.chat.id
    user_modes[user_id] = "checklist"
    await update.message.reply_text("Теперь я буду собирать чек-листы 📋")

# Define message handler
async def handle_message(update: Update, context: CallbackContext):
    if update.message:
        print("\n\n******\n", pending_users, "\n******\n\n")
        user_id = update.message.chat.id
        text = update.message.text or "Сообщение пустое. Напишите что-нибудь"  # Default value if text is None

        # Determine the current mode for the user
        mode = user_modes.get(user_id, default_mode)

        # Check if the user already has a pending message
        with lock:
            if user_id in pending_users:
                await update.message.reply_text(
                    "У вас уже есть сообщение в очереди ⏳. Пожалуйста, дождитесь ответа"
                )
                return

            # Add the user ID to the pending set
            pending_users.add(user_id)

        message_id = update.message.message_id
        message = {
            "id": message_id,
            "user_id": user_id,
            "text": text,
            "mode": mode,  # Add mode to the message
        }

        print(f"Message to be sent: {message}")

        try:
            # Serialize the message to JSON
            message_body = json.dumps(message)

            # Publish message to RabbitMQ
            connection = get_rabbitmq_connection()
            channel = connection.channel()
            channel.queue_declare(queue=QUEUE_NAME)
            channel.basic_publish(exchange="", routing_key=QUEUE_NAME, body=message_body)
            connection.close()

            await update.message.reply_text(
                "Ищу информацию..." if mode == 'search' else 'Собираю чек-лист...'
            )
        except Exception as e:
            # Remove user from pending set if there's an error
            with lock:
                pending_users.discard(user_id)
            await update.message.reply_text(
                "Извините, при обработке вашего вопроса произошла ошибка 👀. Попробуйте еще раз позже"
            )
            print(f"Error adding message to RabbitMQ: {e}")

# REST API Models
class ProcessMessageRequest(BaseModel):
    message_id: int
    reply_text: str
    user_id: int

# REST API Endpoints
@app.get(
    "/get_message",
    summary="Retrieve a message from the queue",
    description="Fetch the next message from the RabbitMQ queue. If the queue is empty, it returns a corresponding message.",
)
async def get_message():
    """
    Fetch the next message from the RabbitMQ queue. 
    If the queue is empty, it returns a message stating no messages are in the queue.
    """
    connection = get_rabbitmq_connection()
    channel = connection.channel()
    channel.queue_declare(queue=QUEUE_NAME)

    method_frame, _, body = channel.basic_get(queue=QUEUE_NAME, auto_ack=True)
    connection.close()

    if body:
        message = json.loads(body)
        user_id = message.get("user_id")

        # Remove the user ID from the pending set
        with lock:
            pending_users.discard(user_id)

        return message
    else:
        return {"message": "No messages in queue"}


@app.post(
    "/process",
    summary="Process a user's message",
    description="Reply to a user's message using the Telegram bot.",
    response_model=dict,
    responses={
        200: {"description": "The message was successfully processed."},
        400: {"description": "Invalid input data."},
    },
)
async def process_message(request: ProcessMessageRequest):
    """
    Process a user's message and send a reply using the Telegram bot.
    """
    # Validate request
    if not request.message_id or not request.reply_text:
        raise HTTPException(status_code=400, detail="Invalid input")

    try:
        # Create the bot instance
        bot = Bot(token=BOT_TOKEN)

        # Send the reply to the user
        await bot.send_message(chat_id=request.user_id, text=request.reply_text)

        return {"status": "Message processed"}
    except Exception as e:
        print(f"Error sending message: {e}")
        raise HTTPException(status_code=500, detail="Failed to send message")

# Run FastAPI in a separate thread
def run_fastapi():
    uvicorn.run(app, host="0.0.0.0", port=API_PORT)

# Main function to set up the bot
def main():
    if not BOT_TOKEN:
        raise ValueError("BOT_TOKEN environment variable is not set")

    # Start FastAPI in a separate thread
    fastapi_thread = threading.Thread(target=run_fastapi, daemon=True)
    fastapi_thread.start()

    # Create the Telegram bot application
    application = Application.builder().token(BOT_TOKEN).build()

    # Register command handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("search", search))
    application.add_handler(CommandHandler("checklist", checklist))

    # Register message handler
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # Start the bot
    application.run_polling()

# Entry point for the application
if __name__ == "__main__":
    main()