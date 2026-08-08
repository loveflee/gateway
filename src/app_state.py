gateway = None
# app_state.py 裡面加上這兩行
from collections import deque
traffic_log = deque(maxlen=60)
