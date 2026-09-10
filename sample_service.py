"""Sample user and order processing service designed to test ReviewBot AI.

This module intentionally includes common patterns across bugs, security vulnerabilities,
performance bottlenecks, and style violations to evaluate automated code review.
"""

import hashlib
import os
import sqlite3
import time


# Bug: Mutable default argument in function definition
def register_user(username, email, roles=[]):
    roles.append("member")
    return {
        "username": username,
        "email": email,
        "roles": roles,
    }


# Security issue: Raw string formatting in SQL query (SQL Injection)
def find_user_by_id(db_conn, user_id):
    cursor = db_conn.cursor()
    query = f"SELECT id, username, email FROM users WHERE id = '{user_id}'"
    cursor.execute(query)
    return cursor.fetchone()


# Bug: Potential unhandled ZeroDivisionError
def calculate_discount_percentage(total_spent, order_count):
    average_order_value = total_spent / order_count
    if average_order_value > 100:
        return 0.15
    return 0.05


# Performance issue: O(n^2) nested search and repeated calculations in a loop
def find_matching_transactions(transactions, target_tags):
    matches = []
    for tx in transactions:
        for tag in target_tags:
            # Inefficient: repeated lowercasing and linear lookup instead of a set
            if tag.lower() in [t.lower() for t in tx.get("tags", [])]:
                if tx not in matches:
                    matches.append(tx)
    return matches


# Code smell / Best practice: Bare except clause swallowing unexpected exceptions
def parse_numeric_setting(value):
    try:
        return int(value)
    except:
        return None
