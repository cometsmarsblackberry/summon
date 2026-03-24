"""Password and secret generation utilities."""

import secrets
import string


def generate_password(length: int = 8) -> str:
    """Generate a random password with mixed case letters and numbers.
    
    Args:
        length: Password length (default 8)
        
    Returns:
        Random password string
    """
    alphabet = string.ascii_letters + string.digits
    # Ensure at least one of each type
    password = [
        secrets.choice(string.ascii_lowercase),
        secrets.choice(string.ascii_uppercase),
        secrets.choice(string.digits),
    ]
    # Fill the rest randomly
    password.extend(secrets.choice(alphabet) for _ in range(length - 3))
    # Shuffle to avoid predictable positions
    password_list = list(password)
    secrets.SystemRandom().shuffle(password_list)
    return "".join(password_list)


def generate_motd_token() -> str:
    """Generate a URL-safe token for MOTD page access.

    Returns:
        43-character URL-safe random string (256 bits of entropy).
    """
    return secrets.token_urlsafe(32)


def generate_logsecret(length: int = 32) -> str:
    """Generate a random secret for logs.tf integration.
    
    Args:
        length: Secret length (default 32)
        
    Returns:
        Random hex string
    """
    return secrets.token_hex(length // 2)
