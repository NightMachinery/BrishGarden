import uvicorn
# from .garden import app

def main():
    uvicorn.run("brishgarden.garden:app", host="127.0.0.1", port=7230, log_level="info", proxy_headers=True)
