tcpdump -i any host 192.168.106.14 and tcp port 502 -nn -XX -l | python3 /root/test/l4.py
