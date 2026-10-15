"""Offline run estimates, independent of fishing controls and game input."""
import math
import re
from decimal import Decimal,ROUND_CEILING


# Recent session: about 160 attempts/hour. Item rates are rounded up from
# a short observed sample; they are editable estimates, not server constants.
CALCULATOR_DEFAULTS=dict(calc_bait=1000,calc_bait_per_hour=160,
                         calc_food_per_hour=1,calc_water_per_hour=1.5,calc_reserve=50)
LIMITS={
    'calc_bait':('Bait amount',1,1_000_000),
    'calc_bait_per_hour':('Bait used per hour',1,10_000),
    'calc_food_per_hour':('Food per hour',.01,1000),
    'calc_water_per_hour':('Water per hour',.01,1000),
    'calc_reserve':('Extra supplies',0,200),
}


def parse_calculator_field(key,value):
    title,low,high=LIMITS[key]
    error=f'{title}: enter '+('a whole number' if key=='calc_bait' else 'a number')+f' from {low:g} to {high:g}.'
    if isinstance(value,bool):raise ValueError(error)
    if key=='calc_bait':
        text=str(value).strip()
        if not re.fullmatch(r'(?:[0-9]+|[1-9][0-9]{0,2}(?:,[0-9]{3})+)',text):raise ValueError(error)
        try:number=int(text.replace(',',''))
        except (ValueError,OverflowError):raise ValueError(error) from None
    else:
        try:number=float(value)
        except (ValueError,TypeError,OverflowError):raise ValueError(error) from None
    if not math.isfinite(number) or not low<=number<=high:raise ValueError(error)
    return number


def calculator_settings(values):
    return {key:parse_calculator_field(key,values.get(key,default))
            for key,default in CALCULATOR_DEFAULTS.items()}


def estimate_run(values):
    values=calculator_settings(values)
    # Decimal arithmetic avoids adding an item at exact boundaries such as
    # 50 hours × 0.14 food/hour (binary floats can produce 7.000000000000001).
    hours=Decimal(values['calc_bait'])/Decimal(str(values['calc_bait_per_hour']))
    food=hours*Decimal(str(values['calc_food_per_hour']))
    water=hours*Decimal(str(values['calc_water_per_hour']))
    reserve=1+Decimal(str(values['calc_reserve']))/100
    up=lambda value:int(value.to_integral_value(rounding=ROUND_CEILING))
    return dict(hours=float(hours),minutes=up(hours*60),food=up(food),water=up(water),
                pack_food=up(food*reserve),pack_water=up(water*reserve))


def estimate_time(minutes):
    hours,minutes=divmod(minutes,60)
    if not hours:return f'{minutes} min'
    return f'{hours} hr'+(f' {minutes} min' if minutes else '')
