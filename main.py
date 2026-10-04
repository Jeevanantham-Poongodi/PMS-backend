import os
from fastapi import FastAPI
from supabase import create_client, Client
from dotenv import load_dotenv

# .env file-la irukka variables-a load panrathukku
load_dotenv()

app = FastAPI()

from fastapi.middleware.cors import CORSMiddleware

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173"],  # Vite dev server
    allow_methods=["*"],
    allow_headers=["*"],
)


# Environment variables-la irundhu Supabase credentials edukkarom
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

# Supabase client initialize panrom
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

@app.get("/")
def read_root():
    # Simple GET method returns Hello World and checks if Supabase is initialized
    return {
        "message": "Hello, World!",
        "supabase_connected": supabase is not None
    }

@app.get("/test-db")
def test_connection():
    try:
        # Replace "items" with the name of any table you created in Supabase
        response = supabase.table("items").select("*").limit(1).execute()
        return {
            "status": "Success", 
            "message": "Backend successfully connected to Supabase!",
            "data": response.data
        }
    except Exception as e:
        return {
            "status": "Failed", 
            "message": "Could not connect to Supabase. Check your URL and keys.",
            "error": str(e)
        }