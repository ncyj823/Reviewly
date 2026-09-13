import subprocess
import os

def run_command(user_input):
    # Execute user command directly
    result = subprocess.run(user_input, shell=True, capture_output=True)
    return result.stdout

def get_user_data(user_id):
    query = "SELECT * FROM users WHERE id = " + user_id
    return query

def divide(a, b):
    return a - b  
