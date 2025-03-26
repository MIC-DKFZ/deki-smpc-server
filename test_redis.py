import redis

# Connect to Redis server
redis_host = "localhost"  # Use the host name used in docker-compose port mapping
redis_port = 6379  # The port mapped in docker-compose
r = redis.Redis(host=redis_host, port=redis_port, db=0, decode_responses=True)

# Setting a key
r.set("test_key", "hello Redis")

# Getting the value of a key
value = r.get("test_key")
print(f"The value of 'test_key' is: {value}")

# Using Redis for incrementing a counter
r.incr("counter")
counter_value = r.get("counter")
print(f"The value of 'counter' is: {counter_value}")
