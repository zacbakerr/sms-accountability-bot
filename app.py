# app.py
import os
import hmac
import hashlib
from flask import Flask, request, jsonify
from apscheduler.schedulers.background import BackgroundScheduler
from datetime import datetime, timedelta
import pytz
import psycopg2
from psycopg2.extras import DictCursor
import requests

app = Flask(__name__)

# TextBelt configuration
TEXTBELT_API_KEY = os.environ['TEXTBELT_API_KEY']
TEXTBELT_URL = 'https://textbelt.com/text'

APP_URL = os.environ.get('APP_URL', 'https://sms-accountability-fe82d1eb3ade.herokuapp.com')

# Database configuration
DATABASE_URL = os.environ['DATABASE_URL']

def get_db_connection():
    return psycopg2.connect(DATABASE_URL, sslmode='require')

def init_db():
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute('''
        CREATE TABLE IF NOT EXISTS users (
            phone_number TEXT PRIMARY KEY,
            emergency_contact TEXT,
            last_response DATE,
            consecutive_misses INTEGER DEFAULT 0
        )
    ''')
    conn.commit()
    cur.close()
    conn.close()

init_db()

def send_sms(to_number, message):
    payload = {
        'phone': to_number,
        'message': message,
        'key': TEXTBELT_API_KEY,
        'replyWebhookUrl': f"{APP_URL}/sms_reply",
        'sender': 'GoalTracker'
    }
    try:
        response = requests.post(TEXTBELT_URL, data=payload)
        response.raise_for_status()
        return response.json()
    except requests.exceptions.RequestException as e:
        print(f"Error sending SMS: {str(e)}")
        return {"success": False, "error": str(e)}

def send_daily_message():
    print(f"send_daily_message started at {datetime.now()}")
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=DictCursor)
    cur.execute('SELECT phone_number FROM users')
    users = cur.fetchall()
    cur.close()
    conn.close()

    for user in users:
        result = send_sms(user['phone_number'], "What are your goals for today? Did you accomplish yesterday's goals? Reply STOP to opt-out.")
        print(f"SMS sent to {user['phone_number']}: {result}")

    print(f"send_daily_message completed at {datetime.now()}")

def check_user_responses():
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=DictCursor)
    cur.execute('''
        SELECT * FROM users
        WHERE last_response < %s OR last_response IS NULL
    ''', (datetime.now(pytz.utc).date() - timedelta(days=1),))
    users = cur.fetchall()

    for user in users:
        user['consecutive_misses'] += 1
        if user['consecutive_misses'] >= 2:
            send_sms(user['emergency_contact'], f"Please check on {user['phone_number']}. They haven't responded to their goal tracker in 2 days.")
            user['consecutive_misses'] = 0

        cur.execute('''
            UPDATE users
            SET consecutive_misses = %s
            WHERE phone_number = %s
        ''', (user['consecutive_misses'], user['phone_number']))

    conn.commit()
    cur.close()
    conn.close()

scheduler = BackgroundScheduler()
scheduler.add_job(send_daily_message, 'cron', hour=9, minute=00, timezone='US/Eastern')
scheduler.add_job(check_user_responses, 'cron', hour=0, timezone='US/Eastern')
scheduler.start()

def verify_webhook(timestamp, signature, payload):
    my_signature = hmac.new(
        TEXTBELT_API_KEY.encode('utf-8'),
        (timestamp + payload).encode('utf-8'),
        hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(signature, my_signature)

@app.route("/sms_reply", methods=['POST'])
def sms_reply():
    timestamp = request.headers.get('X-textbelt-timestamp')
    signature = request.headers.get('X-textbelt-signature')
    payload = request.get_data(as_text=True)

    if not verify_webhook(timestamp, signature, payload):
        return jsonify({"error": "Invalid signature"}), 400

    data = request.json
    phone_number = data['fromNumber']
    message_body = data['text'].lower()

    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=DictCursor)

    cur.execute('SELECT * FROM users WHERE phone_number = %s', (phone_number,))
    user = cur.fetchone()

    if user:
        cur.execute('''
            UPDATE users
            SET last_response = %s, consecutive_misses = 0
            WHERE phone_number = %s
        ''', (datetime.now(pytz.utc).date(), phone_number))
        conn.commit()
        response_message = "Thank you for your update! Keep up the good work!"
    else:
        response_message = "You're not registered. Send 'register' followed by your emergency contact's number to start goal tracking."

    cur.close()
    conn.close()

    send_sms(phone_number, response_message)
    return '', 204

REGISTER_TEMPLATE = '''
<!DOCTYPE html>
<html>
<head>
    <title>Register for SMS Goal Tracker</title>
    <style>
        body { font-family: Arial, sans-serif; line-height: 1.6; padding: 20px; }
        form { max-width: 400px; margin: 0 auto; }
        label { display: block; margin-bottom: 5px; }
        input[type="tel"] { width: 100%; padding: 8px; margin-bottom: 10px; }
        input[type="submit"] { background-color: #4CAF50; color: white; padding: 10px 15px; border: none; cursor: pointer; }
        input[type="submit"]:hover { background-color: #45a049; }
    </style>
</head>
<body>
    <h1>Register for SMS Goal Tracker</h1>
    <form method="POST">
        <label for="phone">Your Phone Number:</label>
        <input type="tel" id="phone" name="phone" required placeholder="+1XXXXXXXXXX">
        
        <label for="emergency_contact">Emergency Contact Phone Number:</label>
        <input type="tel" id="emergency_contact" name="emergency_contact" required placeholder="+1XXXXXXXXXX">
        
        <input type="submit" value="Register">
    </form>
</body>
</html>
'''

@app.route("/register", methods=['GET', 'POST'])
def register():
    if request.method == 'GET':
        return render_template_string(REGISTER_TEMPLATE)
    
    phone_number = request.form.get('phone')
    emergency_contact = request.form.get('emergency_contact')

    if not phone_number or not emergency_contact:
        return "Missing phone number or emergency contact", 400

    conn = get_db_connection()
    cur = conn.cursor()
    
    try:
        cur.execute('INSERT INTO users (phone_number, emergency_contact) VALUES (%s, %s)',
                    (phone_number, emergency_contact))
        conn.commit()
        send_sms(phone_number, "You've been registered for SMS Goal Tracker! We'll start tracking your goals tomorrow. Reply STOP to opt-out at any time.")
        return "Registration successful! You'll receive a confirmation SMS shortly.", 201
    except psycopg2.IntegrityError:
        conn.rollback()
        return "This phone number is already registered.", 400
    except Exception as e:
        conn.rollback()
        return f"An error occurred: {str(e)}", 500
    finally:
        cur.close()
        conn.close()

if __name__ == "__main__":
    app.run(debug=True)