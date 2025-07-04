import os
import json
from prettytable import PrettyTable
from typing import Optional, Tuple
import requests
import finnhub
from llama_index.core.workflow import Context
from utils.httpUtil import get_http_request
from utils.logUtil import setup_logger
from utils.cacheUtil import CacheUtil, StockNewsKeyGenerator, StockPriceKeyGenerator

logger = setup_logger("finUtil")


with open('credentials/finnhub.txt', 'r') as f:
    finnhubKey = f.read().strip()
    finnhubClient = finnhub.Client(api_key=finnhubKey)

with open('credentials/alpha.vantage.txt', 'r') as f:
    alphaVantageKey = f.read().strip()



def get_company_list() -> list:
    #check if data/tickers.csv exists
    if os.path.exists('data/tickers.csv'):
        pass
    else:
        with open("credentials/alpha.vantage.txt", "r") as f:
            alpha_vantage_key = f.read().strip()
        url = f"https://www.alphavantage.co/query?function=LISTING_STATUS&apikey={alpha_vantage_key}"

        response = requests.get(url)
        data = response.text

        logger.info(f"Save ticker list to data/tickers.csv")

        #save first and second columns of data to a csv file in data folder
        with open("data/tickers.csv", "w") as f:
            for line in data.splitlines():
                f.write(",".join(line.split(",")[:2]) + "\n")

    companyList = []
    with open("data/tickers.csv", "r") as f:
        next(f) #skip the first line
        for line in f:
            companyList.append(line.strip().split(","))

    return companyList
    

def get_stock_quote(symbol: str) -> float:
    return finnhubClient.quote(symbol)['c']

    

async def get_stock_prices(ctx: Context, symbol: str, workday: str, previousWorkday: str) -> Tuple[Optional[float], Optional[float]]:
    """
    For a given stock symbol, get the stock price on the given date and price on the previous workday.

    Args:
        ctx (Context): The context between multi-agents.
        symbol (str): The stock symbol.
        workday (str): The date in the format 'YYYY-MM-DD'.
        previousWorkday (str): The date in the format 'YYYY-MM-DD'.

    Returns:
        A tuple of float of the close price of the stock
        on the given date and the previous day.
        If the price is not available, set it to None.
    """
    #check if prices can be got from cache:
    current_state = await ctx.get("state")
    if "stock_price_cache" not in current_state:
        logger.error(f"No stock price cache found in context.")
        return (None, None)
    
    stockPriceCache = current_state["stock_price_cache"]
    workdayData = await stockPriceCache.get(symbol, workday)
    previousWorkdayData = await stockPriceCache.get(symbol, previousWorkday)
    if(workdayData != None and previousWorkdayData != None):
        logger.info(f"Got stock price from cache for {symbol} on {workday} and {previousWorkday}")
        return (workdayData, previousWorkdayData)

    url = f"https://www.alphavantage.co/query?function=TIME_SERIES_DAILY&symbol={symbol}&apikey={alphaVantageKey}"
    try:
        httpData = get_http_request(url=url)
    except Exception as e:
        logger.warning(f"Something went wrong: {e}")
        return (None, None)
    
    #check if httpData has a key 'Information':
    if('Information' in httpData):
        #check if the value contains 'rate limit':
        if('rate limit' in httpData['Information']):
            logger.warning(f"{httpData['Information']}")
            return (None, None)

    #save all valid data to cache
    count = 0
    priceDataDict = httpData['Time Series (Daily)']
    for date in priceDataDict:
        await stockPriceCache.add(float(priceDataDict[date]["4. close"]), symbol, date)
        count += 1
    logger.info(f"Saved {count} stock prices to cache for {symbol}")

    #save prices cache to file:
    await stockPriceCache.save_to_file()

    #save prices cache to context:
    current_state["stock_price_cache"] = stockPriceCache
    await ctx.set("state", current_state)
    
    workdayData = httpData['Time Series (Daily)'].get(workday)
    previousWorkdayData = httpData['Time Series (Daily)'].get(previousWorkday)

    if(not workdayData):
        logger.warning(f"Could not find stock price for {symbol} on {workday}")
        workdayData = 0.0
    else:
        workdayData = float(workdayData["4. close"])
    if(not previousWorkdayData):
        logger.warning(f"Could not find stock price for {symbol} on {previousWorkday}")
        previousWorkdayData = 0.0
    else:
        previousWorkdayData = float(previousWorkdayData["4. close"])

    if(workdayData and previousWorkdayData):
        logger.info(f"Getting price for {symbol} on {workday} and {previousWorkday}: {workdayData}, {previousWorkdayData}")

    return (workdayData, previousWorkdayData)


def format_stock_event_string_to_table(stockEvent: str):
    #get the json format string
    stockEvent = json.loads(stockEvent)

    #get the stock price events
    stock_price_events = stockEvent["stock_price_events"]

    #sort the stock price events by time
    stock_price_events.sort(key=lambda x: x["time"])

    #format the list of events into a table, the table has four columns: time, summary, previous, close
    table = PrettyTable()
    table.field_names = ["Time", "Summary", "Previous", "Close"]
    table._max_width = {"Summary": 50}  # set the max width of "summary" column to 50
    table.hrules = True  # enable horizontal rules
    table.wrap_lines = True  # enable auto wrap of long string
    table.align["Summary"] = "l"  # align "summary" column to left
    for event in stock_price_events:
        table.add_row([event["time"], event["summary"], event["previous"], event["close"]])

    print(table)
    return


async def format_stock_event_string(ctx: Context) -> str:
    """
    For a stock event representd in json format string, format it to a table.

    Args:
        ctx(Context) : The context between multi-agents. The stock event is saved in the context in the following format:
        stockEvent(str) : The stock event representd in json format string. It has the following format:
            {{
            "stock_symbol": companyTicker,
            "past_days": pastDays,
            "stock_total_events": total_number,
            "stock_price_events": [
                {{
                "time": "yyyy-mm-dd",
                "summary": "brief summary of the event",
                "previous": "the stock price of the previous workday. If price is not available, return None",
                "close": "the stock price of the closest workday. If price is not available, return None"
                }}
            ]
            }}

    Returns:
        A string indicating the stock event has been formatted
    """
    current_state = await ctx.get("state")
    if "stock_events" not in current_state:
        logger.error("No stock events found in context.")
        return None

    stockEvent = current_state["stock_events"]
    logger.info(f"Formatting and print stock event")
    format_stock_event_string_to_table(stockEvent)
    return "Stock event formatted and printed"
    



async def save_stock_event_to_cache(stockEvent: str) -> str:
    """
    For a stock event representd in json format string, save it to a cache file.

    Args:
        stockEvent(str) : The stock event representd in json format string. It has the following format:
            {{
            "stock_symbol": companyTicker,
            "past_days": pastDays,
            "stock_total_events": total_number,
            "stock_price_events": [
                {{
                "time": "yyyy-mm-dd",
                "summary": "brief summary of the event",
                "previous": "the stock price of the previous workday. If price is not available, return None",
                "close": "the stock price of the closest workday. If price is not available, return None"
                }}
            ]
            }}

    Returns:
        A string indicating whether the stock event is saved to cache
    """
    stockEvent = json.loads(stockEvent)

    #if stock_price_events is empty, then don't save to cache:
    if(len(stockEvent["stock_price_events"]) == 0):
        return "No stock price events found in stock event"
    
    #save to cache:
    stock_symbol = stockEvent["stock_symbol"]
    past_days = stockEvent["past_days"]
    keyGenerator = StockNewsKeyGenerator()
    stockNewsCache = CacheUtil(100, 'data/stockNewsCache.json', keyGenerator)
    await stockNewsCache.load_cache()

    await stockNewsCache.add(stockEvent, stock_symbol, past_days)
    logger.info(f"Added stock news to cache by: {stock_symbol}, {past_days}")
    await stockNewsCache.save_to_file()
    return "Stock news saved to cache file"


async def load_stock_price_from_cache() -> CacheUtil:
    keyGenerator = StockPriceKeyGenerator()
    stockPriceCache = CacheUtil(1000, 'data/stockPriceCache.json', keyGenerator)
    await stockPriceCache.load_cache()
    return stockPriceCache



# Usage
if __name__ == "__main__":
    #print(get_stock_quote('AAPL'))

    #print(get_stock_prices('TSM', '2025-05-20', '2025-05-19'))

    event_string = """ {"stock_total_events":1,"stock_price_events":[{"time":"2025-05-23","summary":"Oracle will reportedly buy $40 billion worth of Nvidia chips to power the first Stargate project, a new data center in Abilene, Texas. The company will buy 400,000 of Nvidia latest 'superchips' for training and running artificial intelligence (AI) systems...","previous":"132.8300","close":"131.2900"}]}"""

    table = format_stock_event_string_to_table(event_string)