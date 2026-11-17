from django.contrib.auth.models import AbstractUser

class User(AbstractUser):
    """Custom user from day 1; admin permissions provided by Django."""

    class Meta:
        db_table = "accounts_user"
