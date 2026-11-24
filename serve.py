import os

from waitress import serve

from santa import create_app


if __name__ == "__main__":
    serve(create_app(), host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
