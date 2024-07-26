import json
import os

import numpy as np
import pandas as pd
import quantstats as qs
import requests
import yahooquery as yq

from cache import file_cache

# https://www.bankofcanada.ca/rates/interest-rates/corra/
# updated July 13, 2024
CANADA_RISK_FREE_RATE = 4.80
# 1Y rate of return for S&P/TSX Composite Index
# https://ycharts.com/indices/%5ETSX
# updated July 13, 2024
TSX_EXPECTED_RETURN = 16.69


def save(obj, filename):
    with open(filename, 'w') as json_file:
        json.dump(obj, json_file)


def fetch(filename):
    if not os.path.exists(filename):
        return None
    with open(filename, 'r') as json_file:
        return json.load(json_file)


@file_cache('companies.json')
def get_companies_from_tsx():
    companies = set()
    tsx_raw = requests.get("https://www.tsx.com/json/company-directory/search/tsx/^*").json()
    for tsx in tsx_raw['results']:
        companies.add(tsx['symbol'])
        for instrument in tsx['instruments']:
            companies.add(instrument['symbol'])
    return companies


def get_companies():
    companies = get_companies_from_tsx()
    data = fetch('output/data.json')
    if data:
        companies = filter(lambda c: c not in dict(data).keys(), companies)
    no_data = fetch('no_data.json')
    if no_data:
        companies = filter(lambda c: c not in list(no_data), companies)
    return list(sorted(companies))


def save_data(data, fileprefix=''):
    if not data:
        return
    if not os.path.exists("output"):
        os.mkdir("output")
    existing = fetch('output/' + fileprefix + 'data.json')
    if existing:
        save({**dict(existing), **data}, 'output/' + fileprefix + 'data.json')
    else:
        save(data, 'output/' + fileprefix + 'data.json')


def get_risk(symbol):
    returns = qs.utils.download_returns(symbol)
    if returns is None or not isinstance(returns, pd.Series):
        raise ValueError('cannot find risk - ' + symbol)
    var = qs.stats.var(returns)
    cvar = qs.stats.cvar(returns)
    if var is None or np.isnan(var) or cvar is None or np.isnan(cvar):
        raise ValueError('cannot find risk - ' + symbol)
    return var, cvar


def get_esg(symbol, ticker):
    environment, social, governance = np.nan, np.nan, np.nan
    if not isinstance(ticker.esg_scores.get(symbol), str):
        environment = ticker.esg_scores.get(symbol).get('environmentScore')
        governance = ticker.esg_scores.get(symbol).get('governanceScore')
        social = ticker.esg_scores.get(symbol).get('socialScore')
    return environment, governance, social


def get_capm_expected_return(symbol, ticker):
    if not isinstance(ticker.summary_detail.get(symbol), dict):
        raise ValueError('no expected return - ' + symbol)
    beta = ticker.summary_detail.get(symbol).get('beta')
    if beta is None or np.isnan(beta):
        raise ValueError('no expected return - ' + symbol)
    return CANADA_RISK_FREE_RATE + (beta * (TSX_EXPECTED_RETURN - CANADA_RISK_FREE_RATE))


def get_price(symbol, ticker):
    if (not isinstance(ticker.price.get(symbol), dict)
            or 'regularMarketPreviousClose' not in ticker.price.get(symbol).keys()
            or np.isnan(ticker.price.get(symbol).get('regularMarketPreviousClose'))):
        raise ValueError('no price - ' + symbol)
    price = ticker.price.get(symbol).get('regularMarketPreviousClose')
    return price


def get_symbol(company, refresh_cache=True):
    result = yq.search(company, country='canada', first_quote=True)
    if 'symbol' not in result.keys():
        if refresh_cache:
            with open('companies.json', 'r') as json_file:
                companies = list(json.load(json_file))
                companies.remove(company)
            with open('companies.json', 'w') as json_file:
                json.dump(companies, json_file)
        raise ValueError('symbol not found - ' + company)
    symbol = result['symbol']
    if symbol != company and refresh_cache:
        with open('companies.json', 'r') as json_file:
            companies = list(json.load(json_file))
            companies[companies.index(company)] = symbol
        with open('companies.json', 'w') as json_file:
            json.dump(companies, json_file)
    return symbol


def get_company_data(companies, retry, no_data, fileprefix='', refresh_cache=True):
    data = {}
    success_count = 0
    esg_count = 0
    for company in companies:
        print("Gathering output for " + company)
        try:
            symbol = get_symbol(company, refresh_cache)
            ticker = yq.Ticker(symbol)
            price = get_price(symbol, ticker)
            expected_return = get_capm_expected_return(symbol, ticker)
            cvar, var = get_risk(symbol)
            environment, governance, social = get_esg(symbol, ticker)
            data[symbol] = {
                'ticker': symbol,
                'price': price,
                'return': expected_return,
                'cvar': cvar,
                'var': var,
                'environment': None if environment is None or np.isnan(environment) else environment,
                'governance': None if governance is None or np.isnan(governance) else governance,
                'social': None if social is None or np.isnan(social) else social,
            }
            success_count += 1
            if data[symbol]['environment'] or data[symbol]['governance'] or data[symbol]['social']:
                esg_count += 1
            save_data(data, fileprefix)
            print("currently " + str(success_count) + " valid data points with " + str(esg_count) + ' esg data points')
        except ValueError as e:
            print(e)
            no_data.append(company)
            curr_no_data = fetch('no_data.json')
            if curr_no_data:
                save(list(set(list(curr_no_data) + no_data)), 'no_data.json')
            else:
                save(list(set(no_data)), 'no_data.json')
            continue
        except Exception as e:
            print(e)
            retry.append(company)
            print('adding "' + company + '" to retries. currently ' + str(len(retry)) + ' retries in the queue')
            continue
    return data, retry, no_data


def save_company_data(companies, fileprefix='', refresh_cache=True):
    data, retry, no_data = get_company_data(companies, [], [], fileprefix, refresh_cache)
    save(no_data, fileprefix + 'no_data.json')
    attempts = 1
    while len(retry) > 0 and attempts < 5:
        print('attempt: ' + str(attempts) + ' | number of failed fetches: ' + str(len(retry)))
        new_data, new_failed_companies, no_data = get_company_data(retry, [], no_data)
        data = {**data, **new_data}
        retry = new_failed_companies
    return data


def scale_esg():
    with open('output/data.json', 'r') as json_file:
        data = dict(json.load(json_file))
        max = {
            'environment': 0,
            'social': 0,
            'governance': 0,
        }
        for v in data.values():
            if v['environment'] and v['environment'] > max['environment']:
                max['environment'] = v['environment']
            if v['social'] and v['social'] > max['social']:
                max['social'] = v['social']
            if v['governance'] and v['governance'] > max['governance']:
                max['governance'] = v['governance']
        for k in data:
            if data[k]['environment']:
                data[k]['environment'] = data[k]['environment'] / float(max['environment'])
            if data[k]['social']:
                data[k]['social'] = data[k]['social'] / float(max['social'])
            if data[k]['governance']:
                data[k]['governance'] = data[k]['governance'] / float(max['governance'])
    with open('output/data.json', 'w') as json_file:
        json.dump(data, json_file)


def main():
    companies = get_companies()
    save_company_data(companies)
    scale_esg()
    index = ['GSPTSE']
    save_company_data(index, 'index-', False)


if __name__ == "__main__":
    main()
