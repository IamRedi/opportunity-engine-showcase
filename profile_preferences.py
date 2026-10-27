"""Reuse explicit preference statements; never infer a car from a licence."""
import re


def local_values(entries):
    data={k:v for entry in entries for k,v in entry.items() if isinstance(v,str)}
    out={}
    if data.get('availability'):out['available_days']=data['availability']
    travel=data.get('travel','').casefold()
    for pattern,key in [(r'(\d{1,3})\s*km\b','radius_km'),(r'(\d{1,3})\s*(?:min|minutes|minuten|minuta)\b','max_travel_min')]:
        match=re.search(pattern,travel)
        if match:out[key]=int(match[1])
    salary=data.get('salary','').casefold()
    match=re.search(r'(\d{1,3}(?:[.,]\d{1,2})?)\s*(?:€|eur|euro)\s*(?:/\s*(?:h|hour|std|orë)|(?:per|pro|në|ne)\s+(?:hour|stunde|orë))\b',salary)
    if match:out['min_hourly_rate']=float(match[1].replace(',','.'))
    transport=data.get('transport','').casefold()
    if re.search(r'\b(?:no car|without a car|kein(?:e|en)? auto|ohne auto|nuk kam makinë|nuk kam makine|pa makinë|pa makine)\b',transport):out['no_car']=True
    elif re.search(r'\b(?:own car|have a car|eigenes auto|eigenen pkw|kam makinë|kam makine)\b',transport):out['transport_mode']='car'
    elif re.search(r'\b(?:public transport|öffentliche verkehrsmittel|transport publik)\b',transport):out['transport_mode']='public transport'
    return out
