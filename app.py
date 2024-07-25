import os
from flask import request, render_template, jsonify
from twilio.rest import Client
from twilio.twiml.messaging_response import MessagingResponse
from apscheduler.schedulers.background import BackgroundScheduler
from datetime import datetime, timedelta
import pytz
from database import db, User
from dotenv import load_dotenv

load_dotenv()  # Load environment variables from .env file

app = Flask(__name__)
app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get('DATABASE_URL').replace("://", "ql://", 1)
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db.init_app(app)

# Twilio credentials
account_sid = os.getenv('TWILIO_ACCOUNT_SID')
auth_token = os.getenv('TWILIO_AUTH_TOKEN')
client = Client(account_sid, auth_token)

@app.route("/sms", methods=['POST'])
def sms_reply():
    """Respond to incoming messages."""
    body = request.values.get('Body', None)
    from_number = request.values.get('From', None)

    resp = MessagingResponse()
    resp.message("Got your message! Processing now...")

    process_message(from_number, body)

    return str(resp)

def process_message(phone_number, message):
    """Process incoming messages and update user data."""
    user = User.query.filter_by(phone_number=phone_number).first()
    if not user:
        user = User(phone_number=phone_number)
        db.session.add(user)
    
    user.last_response = datetime.now(pytz.timezone('US/Eastern'))
    user.goals_completed = 'yes' in message.lower()
    db.session.commit()

def send_daily_message():
    """Send daily message to all users at 8 AM EST."""
    users = User.query.all()
    for user in users:
        message = client.messages.create(
            body="Did you accomplish yesterday's goals? What are your goals for today?",
            from_=os.getenv('TWILIO_PHONE_NUMBER'),
            to=user.phone_number
        )

def check_user_status():
    """Check if users haven't responded or completed goals for 2 days."""
    est = pytz.timezone('US/Eastern')
    now = datetime.now(est)
    two_days_ago = now - timedelta(days=2)

    users = User.query.filter((User.last_response < two_days_ago) | (User.goals_completed == False)).all()
    for user in users:
        if user.contact_number:
            message = client.messages.create(
                body=f"Please check in on {user.phone_number}. They haven't been responding to their goal tracking messages.",
                from_=os.getenv('TWILIO_PHONE_NUMBER'),
                to=user.contact_number
            )

@app.route("/register", methods=['GET', 'POST'])
def register_user():
    if request.method == 'POST':
        phone_number = request.form.get('phone_number')
        contact_number = request.form.get('contact_number')
        
        if not phone_number or not contact_number:
            return jsonify({"error": "Both phone number and contact number are required"}), 400
        
        user = User.query.filter_by(phone_number=phone_number).first()
        if user:
            return jsonify({"error": "User already registered"}), 400
        
        new_user = User(phone_number=phone_number, contact_number=contact_number)
        db.session.add(new_user)
        db.session.commit()
        
        return jsonify({"message": "User registered successfully"}), 200
    
    # If it's a GET request, render a simple HTML form
    return '''
    <form method="post">
        <label for="phone_number">Phone Number:</label><br>
        <input type="text" id="phone_number" name="phone_number" required><br>
        <label for="contact_number">Contact Number:</label><br>
        <input type="text" id="contact_number" name="contact_number" required><br>
        <input type="submit" value="Register">
    </form>
    '''

def create_tables():
    with app.app_context():
        db.create_all()

if __name__ == "__main__":
    with app.app_context():
        db.create_all()  # Create database tables
    
    scheduler = BackgroundScheduler(timezone=pytz.timezone('US/Eastern'))
    scheduler.add_job(send_daily_message, 'cron', hour=8, minute=0)
    scheduler.add_job(check_user_status, 'interval', hours=12)
    scheduler.start()
    create_tables()
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))