import subprocess

print("Starting BUY bot...")
subprocess.Popen(["python", "bot.py"])

print("Starting SELL bot...")
subprocess.Popen(["python", "sell_bot.py"])

# يخلي البرنامج شغال
while True:
    pass
