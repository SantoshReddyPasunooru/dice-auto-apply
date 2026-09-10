#!/bin/bash
set -e

echo "Pulling latest changes..."
git pull origin main

echo "Installing dependencies..."
pip install -r requirements.txt --quiet

echo "Updating Playwright browsers..."
playwright install chromium --quiet

echo ""
echo "✓ Update complete."
echo "  Run: python web_dashboard.py --profile your@gmail.com"
