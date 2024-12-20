import os
import time
from flask import Flask, render_template, jsonify, request, send_file, abort, send_from_directory
from flask_mail import Mail, Message
from decimal import Decimal
from models import db, Bill, Diagnosis, Procedure, InsuranceClaim
from datetime import datetime, timedelta
import logging
import requests
from requests.exceptions import RequestException
from pdf_generator import generate_bill_pdf
import json

app = Flask(__name__)

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = app.logger

# Database configuration
app.config["SQLALCHEMY_DATABASE_URI"] = os.environ.get("DATABASE_URL")
app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {
    "pool_recycle": 300,
    "pool_pre_ping": True,
}
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

# Email configuration
app.config['MAIL_SERVER'] = 'smtp.gmail.com'
app.config['MAIL_PORT'] = 587
app.config['MAIL_USE_TLS'] = True
app.config['MAIL_USERNAME'] = os.environ.get('MAIL_USERNAME')
app.config['MAIL_PASSWORD'] = os.environ.get('MAIL_PASSWORD')

# Initialize extensions
mail = Mail(app)
db.init_app(app)

# Initialize database tables
with app.app_context():
    try:
        db.create_all()
        logger.info("Database tables created successfully")
    except Exception as e:
        logger.error(f"Database error: {str(e)}")

# Static exchange rates for fallback
STATIC_EXCHANGE_RATES = {
    'USD': 1.0,
    'EUR': 0.85,
    'GBP': 0.73,
    'JPY': 110.0,
    'CAD': 1.25,
    'AUD': 1.35,
    'CNY': 6.45
}

# Static crypto prices for fallback
STATIC_CRYPTO_PRICES = {
    'BTC': 35000.00,
    'ETH': 2000.00,
    'USDT': 1.00,
    'USDC': 1.00
}

# Cache durations and retry settings
CACHE_DURATION = 30  # seconds
MAX_RETRIES = 3
RETRY_DELAY = 1  # seconds
REQUEST_TIMEOUT = 5  # seconds

# Cache variables
last_rates_update = 0
last_crypto_update = 0
cached_exchange_rates = None
cached_crypto_prices = None

def get_live_exchange_rates():
    """Fetch live exchange rates from an API with retry mechanism"""
    for attempt in range(MAX_RETRIES):
        try:
            response = requests.get(
                'https://api.exchangerate-api.com/v4/latest/USD',
                timeout=REQUEST_TIMEOUT
            )
            response.raise_for_status()
            data = response.json()
            
            rates = {
                'USD': 1.0,
                'EUR': data['rates'].get('EUR', STATIC_EXCHANGE_RATES['EUR']),
                'GBP': data['rates'].get('GBP', STATIC_EXCHANGE_RATES['GBP']),
                'JPY': data['rates'].get('JPY', STATIC_EXCHANGE_RATES['JPY']),
                'CAD': data['rates'].get('CAD', STATIC_EXCHANGE_RATES['CAD']),
                'AUD': data['rates'].get('AUD', STATIC_EXCHANGE_RATES['AUD']),
                'CNY': data['rates'].get('CNY', STATIC_EXCHANGE_RATES['CNY'])
            }
            
            logger.info("Successfully fetched live exchange rates")
            return rates
            
        except requests.exceptions.RequestException as e:
            logger.error(f"Attempt {attempt + 1}/{MAX_RETRIES} failed: {str(e)}")
            if attempt < MAX_RETRIES - 1:
                time.sleep(RETRY_DELAY)
            continue
        except Exception as e:
            logger.error(f"Unexpected error fetching exchange rates: {str(e)}")
            break
            
    logger.warning("All attempts to fetch exchange rates failed, using static rates")
    return None

def get_live_crypto_prices():
    """Fetch live cryptocurrency prices from Coinbase API with retry mechanism"""
    coinbase_key = os.environ.get('COINBASE_COMMERCE_API_KEY')
    if not coinbase_key:
        logger.error("Coinbase API key not found in environment variables")
        return None

    headers = {
        'X-CC-Api-Key': coinbase_key,
        'X-CC-Version': '2018-03-22'
    }
    
    crypto_symbols = ['BTC', 'ETH', 'USDT', 'USDC']
    prices = {}
    
    for symbol in crypto_symbols:
        for attempt in range(MAX_RETRIES):
            try:
                response = requests.get(
                    f'https://api.commerce.coinbase.com/v2/exchange-rates?currency={symbol}',
                    headers=headers,
                    timeout=REQUEST_TIMEOUT
                )
                response.raise_for_status()
                data = response.json()
                
                if 'data' in data and 'rates' in data['data'] and 'USD' in data['data']['rates']:
                    prices[symbol] = float(data['data']['rates']['USD'])
                    logger.info(f"Successfully fetched {symbol} price")
                    break
                else:
                    raise ValueError(f"Invalid response format for {symbol}")
                    
            except requests.exceptions.RequestException as e:
                logger.error(f"Attempt {attempt + 1}/{MAX_RETRIES} failed for {symbol}: {str(e)}")
                if attempt < MAX_RETRIES - 1:
                    time.sleep(RETRY_DELAY)
                continue
            except ValueError as e:
                logger.error(f"Invalid response for {symbol}: {str(e)}")
                break
            except Exception as e:
                logger.error(f"Unexpected error fetching {symbol} price: {str(e)}")
                break
                
        if symbol not in prices:
            logger.warning(f"Using fallback price for {symbol}")
            prices[symbol] = STATIC_CRYPTO_PRICES[symbol]
    
    return prices if len(prices) == len(crypto_symbols) else None

@app.route('/')
def index():
    try:
        logger.info("Rendering index page")
        return render_template('index.html')
    except Exception as e:
        logger.error(f"Error rendering template: {str(e)}")
        return f"Error loading page: {str(e)}", 500

@app.route('/static/<path:filename>')
def serve_static(filename):
    return send_from_directory('static', filename)

@app.route('/dashboard')
def dashboard():
    try:
        payments = Bill.query.order_by(Bill.created_at.desc()).all()
        return render_template('dashboard.html', payments=payments)
    except Exception as e:
        logger.error(f"Error loading dashboard: {str(e)}")
        return f"Error loading dashboard: {str(e)}", 500

@app.route('/get_exchange_rates')
def get_exchange_rates():
    global last_rates_update, cached_exchange_rates
    
    try:
        current_time = time.time()
        
        if cached_exchange_rates is None or (current_time - last_rates_update) >= CACHE_DURATION:
            rates = get_live_exchange_rates()
            if rates:
                cached_exchange_rates = rates
                last_rates_update = current_time
                logger.info("Exchange rates cache updated")
            else:
                logger.warning("Using fallback exchange rates")
                cached_exchange_rates = STATIC_EXCHANGE_RATES
                
        return jsonify({
            'success': True,
            'rates': cached_exchange_rates,
            'is_live': cached_exchange_rates != STATIC_EXCHANGE_RATES,
            'timestamp': last_rates_update
        })
    except Exception as e:
        logger.error(f"Error in exchange rates endpoint: {str(e)}")
        return jsonify({
            'success': False,
            'error': "Failed to fetch exchange rates",
            'rates': STATIC_EXCHANGE_RATES,
            'is_live': False
        })

@app.route('/get_crypto_prices')
def get_crypto_prices():
    global last_crypto_update, cached_crypto_prices
    
    try:
        current_time = time.time()
        
        if cached_crypto_prices is None or (current_time - last_crypto_update) >= CACHE_DURATION:
            prices = get_live_crypto_prices()
            if prices:
                cached_crypto_prices = prices
                last_crypto_update = current_time
                logger.info("Crypto prices cache updated")
            else:
                logger.warning("Using fallback crypto prices")
                cached_crypto_prices = STATIC_CRYPTO_PRICES
                
        return jsonify({
            'success': True,
            'prices': cached_crypto_prices,
            'is_live': cached_crypto_prices != STATIC_CRYPTO_PRICES,
            'timestamp': last_crypto_update
        })
    except Exception as e:
        logger.error(f"Error in crypto prices endpoint: {str(e)}")
        return jsonify({
            'success': False,
            'error': "Failed to fetch cryptocurrency prices",
            'prices': STATIC_CRYPTO_PRICES,
            'is_live': False
        })

@app.template_filter('status_badge')
def status_badge(status):
    badges = {
        'pending': 'warning',
        'paid': 'success',
        'failed': 'danger'
    }
    return badges.get(status, 'secondary')

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
