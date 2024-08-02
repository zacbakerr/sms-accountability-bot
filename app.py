import os
from flask import Flask, request, jsonify, render_template_string
from apscheduler.schedulers.background import BackgroundScheduler
from datetime import datetime, timedelta
import psycopg2
from psycopg2.extras import DictCursor
import requests
import json
import anthropic
import re

app = Flask(__name__)

# Configuration
TEXTBELT_API_KEY = os.environ.get('TEXTBELT_API_KEY')
TEXTBELT_URL = 'https://textbelt.com/text'
APP_URL = os.environ.get('APP_URL', 'https://your-app-name.herokuapp.com')
DATABASE_URL = os.environ['DATABASE_URL']
CLAUDE_API_KEY = os.environ.get('CLAUDE_API_KEY')

claude_client = anthropic.Anthropic(api_key=CLAUDE_API_KEY)

def get_db_connection():
  return psycopg2.connect(DATABASE_URL, sslmode='require')

def init_db():
  conn = get_db_connection()
  cur = conn.cursor()
  try:
    # Create users table
    cur.execute('''
      CREATE TABLE IF NOT EXISTS users (
        phone_number TEXT PRIMARY KEY,
        emergency_contact TEXT,
        last_response DATE
      )
    ''')

    # Create daily_goals table
    cur.execute('''
      CREATE TABLE IF NOT EXISTS daily_goals (
        id SERIAL PRIMARY KEY,
        phone_number TEXT REFERENCES users(phone_number),
        date DATE,
        goals TEXT[],
        completion_status BOOLEAN[]
      )
    ''')

    # Check if the index exists before creating it
    cur.execute('''
      SELECT 1
      FROM pg_indexes
      WHERE indexname = 'idx_daily_goals_phone_date'
    ''')

    if cur.fetchone() is None:
      # Create the index if it doesn't exist
      cur.execute('''
        CREATE UNIQUE INDEX idx_daily_goals_phone_date 
        ON daily_goals (phone_number, date)
      ''')
      print("Created index idx_daily_goals_phone_date")
    else:
      print("Index idx_daily_goals_phone_date already exists")

    conn.commit()
    print("Database initialization completed successfully")
  except Exception as e:
    conn.rollback()
    print(f"Error during database initialization: {str(e)}")
  finally:
    cur.close()
    conn.close()

init_db()

def send_sms(to_number, message):
  payload = {
    'phone': to_number,
    'message': message,
    'key': TEXTBELT_API_KEY,
    'replyWebhookUrl': f"{APP_URL}/sms_reply"
  }
  try:
    response = requests.post(TEXTBELT_URL, data=payload)
    response.raise_for_status()
    return response.json()
  except requests.exceptions.RequestException as e:
    print(f"Error sending SMS: {str(e)}")
    return {"success": False, "error": str(e)}

def send_morning_message():
  conn = get_db_connection()
  cur = conn.cursor(cursor_factory=DictCursor)
  cur.execute('SELECT phone_number FROM users')
  users = cur.fetchall()
  cur.close()
  conn.close()

  for user in users:
    message = "Good morning! What are your goals for today? Please separate them with commas."
    send_sms(user['phone_number'], message)

def send_evening_followup():
  conn = get_db_connection()
  cur = conn.cursor(cursor_factory=DictCursor)
  cur.execute('SELECT phone_number FROM users')
  users = cur.fetchall()
  cur.close()
  conn.close()

  yesterday = datetime.now().date() - timedelta(days=1)
  for user in users:
    yesterday_goals = get_user_goals(user['phone_number'], yesterday)
    if yesterday_goals and yesterday_goals['goals']:
      goals_list = ', '.join(yesterday_goals['goals'])
      message = f"Yesterday, your goals were: {goals_list}. Did you meet them? Please respond with Yes/No for each goal, separated by commas."
      send_sms(user['phone_number'], message)

def check_inactivity_and_notify():
  conn = get_db_connection()
  cur = conn.cursor(cursor_factory=DictCursor)

  # Get users who haven't responded in the last 3 days
  three_days_ago = datetime.now().date() - timedelta(days=3)
  cur.execute('''
    SELECT u.phone_number, u.emergency_contact, u.last_response
    FROM users u
    WHERE u.last_response < %s OR u.last_response IS NULL
  ''', (three_days_ago,))

  inactive_users = cur.fetchall()

  for user in inactive_users:
    # Prepare message for emergency contact
    days_inactive = (datetime.now().date() - user['last_response']).days if user['last_response'] else 'several'
    message = f"Emergency Alert: {user['phone_number']} has been inactive for {days_inactive} days on their goal tracking app."

    # Send message to emergency contact
    send_sms(user['emergency_contact'], message)

    print(f"Sent inactivity alert for {user['phone_number']} to {user['emergency_contact']}")

  cur.close()
  conn.close()

def store_user_goals(phone_number, date, goals):
  conn = get_db_connection()
  cur = conn.cursor()
  goals_json = json.dumps(goals)  # Convert goals array to JSON string
  cur.execute('''
    INSERT INTO daily_goals (phone_number, date, goals, completion_status)
    VALUES (%s, %s, %s, %s)
    ON CONFLICT (phone_number, date) 
    DO UPDATE SET goals = EXCLUDED.goals
  ''', (phone_number, date, goals_json, json.dumps([False] * len(goals))))
  conn.commit()
  cur.close()
  conn.close()

def get_user_goals(phone_number, date):
  conn = get_db_connection()
  cur = conn.cursor(cursor_factory=DictCursor)
  cur.execute('''
    SELECT goals, completion_status 
    FROM daily_goals 
    WHERE phone_number = %s AND date = %s
  ''', (phone_number, date))
  result = cur.fetchone()
  cur.close()
  conn.close()
  if result:
    return {
      'goals': json.loads(result['goals']),
      'completion_status': json.loads(result['completion_status'])
    }
  return None

def update_goal_completion(phone_number, date, completion_status):
  conn = get_db_connection()
  cur = conn.cursor()
  cur.execute('''
    UPDATE daily_goals
    SET completion_status = %s
    WHERE phone_number = %s AND date = %s
  ''', (json.dumps(completion_status), phone_number, date))
  conn.commit()
  cur.close()
  conn.close()

@app.route("/sms_reply", methods=['POST'])
def sms_reply():
  data = request.json
  phone_number = data['fromNumber']
  message_body = data['text']

  today = datetime.now().date()
  yesterday = today - timedelta(days=1)

  # Check if this is a response to the morning message (setting goals)
  if "," in message_body and not any(word in message_body.lower() for word in ['yes', 'no']):
    goals = [goal.strip() for goal in message_body.split(',')]
    store_user_goals(phone_number, today, goals)
    response = "Thanks for sharing your goals. I've saved them and will check in with you later!"

  # Check if this is a response to the evening follow-up (goal completion)
  elif any(word in message_body.lower() for word in ['yes', 'no']):
    yesterday_goals = get_user_goals(phone_number, yesterday)
    if yesterday_goals:
      responses = [r.strip().lower() for r in message_body.split(',')]
      completion_status = [r == 'yes' for r in responses]
      update_goal_completion(phone_number, yesterday, completion_status)
      response = "Thanks for the update! Keep up the good work and let's focus on today's goals."
    else:
      response = "I'm sorry, I couldn't find your goals from yesterday. Let's focus on setting new goals for today!"

  else:
    # Use Claude to respond like a helpful assistant
    yesterday_goals = get_user_goals(phone_number, yesterday)
    if yesterday_goals:
        goals_context = ', '.join(yesterday_goals['goals'])
        assistant_prompt = f"The user's goals are: {goals_context}. They sent: '{message_body}'. Respond like a helpful assistant trying to help them accomplish their goals and keep them on task."
    else:
        assistant_prompt = f"The user sent: '{message_body}'. Respond like a helpful assistant trying to help them accomplish their goals and keep them on task."

    claude_response = claude_client.completions.create(
        model="claude-2",
        prompt=f"{anthropic.HUMAN_PROMPT} {assistant_prompt} {anthropic.AI_PROMPT}",
        max_tokens_to_sample=100,
        temperature=0.7
    )
    response = claude_response.completion

  send_sms(phone_number, response)

  # Update last_response date
  conn = get_db_connection()
  cur = conn.cursor()
  cur.execute('UPDATE users SET last_response = %s WHERE phone_number = %s', (datetime.now().date(), phone_number))
  conn.commit()
  cur.close()
  conn.close()

  return '', 204

REGISTER_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Register for GoalMaster AI</title>
  <style>
    body {
      font-family: 'Arial', sans-serif;
      background: linear-gradient(135deg, #1a1a2e 0%, #16213e 100%);
      color: #ffffff;
      display: flex;
      justify-content: center;
      align-items: center;
      height: 100vh;
      margin: 0;
    }
    .container {
      background: rgba(0, 0, 0, 0.6);
      padding: 20px;
      border-radius: 10px;
      box-shadow: 0 0 20px rgba(0, 0, 0, 0.5);
      width: 100%;
      max-width: 500px;
      text-align: center;
    }
    input[type="text"], input[type="submit"] {
      width: 100%;
      padding: 10px;
      margin: 10px 0;
      border: none;
      border-radius: 5px;
    }
    input[type="submit"] {
      background-color: #28a745;
      color: white;
      cursor: pointer;
      transition: background-color 0.3s ease;
    }
    input[type="submit"]:hover {
      background-color: #218838;
    }
  </style>
</head>
<body>
  <div class="container">
    <h1>Register for GoalMaster AI</h1>
    <form action="/register" method="POST">
      <input type="text" name="phone_number" placeholder="Your Phone Number" required>
      <input type="text" name="emergency_contact" placeholder="Emergency Contact Number" required>
      <input type="submit" value="Register">
    </form>
  </div>
</body>
</html>
"""

@app.route('/')
def register():
  return render_template_string(REGISTER_TEMPLATE)

@app.route('/register', methods=['POST'])
def register_user():
  phone_number = request.form['phone_number']
  emergency_contact = request.form['emergency_contact']

  conn = get_db_connection()
  cur = conn.cursor()
  cur.execute('''
    INSERT INTO users (phone_number, emergency_contact, last_response)
    VALUES (%s, %s, %s)
    ON CONFLICT (phone_number) DO NOTHING
  ''', (phone_number, emergency_contact, datetime.now().date()))
  conn.commit()
  cur.close()
  conn.close()

  welcome_message = (
    "Welcome to GoalMaster AI! We're here to help you achieve your daily goals. "
    "Each morning, we'll ask for your goals, and each evening, we'll check in on your progress."
  )
  send_sms(phone_number, welcome_message)

  return "Registration successful! A welcome message has been sent to your phone."

if __name__ == '__main__':
  scheduler = BackgroundScheduler()
  scheduler.add_job(send_morning_message, 'cron', hour=9, minute=0)
  scheduler.add_job(send_evening_followup, 'cron', hour=18, minute=0)
  scheduler.add_job(check_inactivity_and_notify, 'cron', hour=12, minute=0)
  scheduler.start()

  app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))
