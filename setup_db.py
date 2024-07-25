from app import app, db

def setup_database():
    with app.app_context():
        db.create_all()
        print("Database tables created successfully.")

if __name__ == "__main__":
    setup_database()