SENT = []
def send_message(*args, **kwargs):
    SENT.append((args, kwargs))
    return {"result": {"message_id": 1}}
