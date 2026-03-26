#!/bin/bash

echo "Starting BUY bot..."
python bot.py &

echo "Starting SELL bot..."
python sell_bot.py

wait
