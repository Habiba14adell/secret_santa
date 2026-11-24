import getpass

from werkzeug.security import generate_password_hash


if __name__ == "__main__":
    password = getpass.getpass("Admin password (at least 16 characters): ")
    if len(password) < 16:
        raise SystemExit("Use at least 16 characters.")
    if password != getpass.getpass("Confirm admin password: "):
        raise SystemExit("Passwords differ.")
    print(generate_password_hash(password))
