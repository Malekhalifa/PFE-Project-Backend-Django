"""
Custom User model — stored in PostgreSQL via Django ORM.

Mirrors the Node.js/Mongoose User schema:
  - UUID primary key
  - email as the login identifier (USERNAME_FIELD)
  - role field: 'user' or 'admin'
  - password hashed via Django's built-in auth (bcrypt-compatible)
"""
import uuid

from django.contrib.auth.models import AbstractUser
from django.db import models


class User(AbstractUser):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    email = models.EmailField(unique=True)
    role = models.CharField(
        max_length=10,
        choices=[('user', 'user'), ('admin', 'admin')],
        default='user',
    )

    USERNAME_FIELD = 'email'
    REQUIRED_FIELDS = ['username']

    class Meta:
        db_table = 'users'

    def __str__(self):
        return self.email
