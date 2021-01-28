
import base64
import hashlib
import hmac
import json
import time
import httplib2

import secret


def get_encoded_payload(payload):
    payload['nonce'] = int(time.time() * 1000)

    dumped_json = json.dumps(payload)
    encoded_json = base64.b64encode(bytes(dumped_json, 'utf-8'))
    return encoded_json


def get_signature(encoded_payload):
    signature = hmac.new(secret.SECRET_KEY, encoded_payload, hashlib.sha512)
    return signature.hexdigest()


def get_response(action, payload):
    url = '{}{}'.format('https://api.coinone.co.kr/', action)

    encoded_payload = get_encoded_payload(payload)

    headers = {
        'Content-type': 'application/json',
        'X-COINONE-PAYLOAD': encoded_payload,
        'X-COINONE-SIGNATURE': get_signature(encoded_payload),
    }

    http = httplib2.Http()
    response, content = http.request(url, 'POST', body=encoded_payload, headers=headers)

    return content



# access token, secret key 를 가지고
# 실제로 지정된 화폐를 거래할 수 있도록 하는 API
# https://doc.coinone.co.kr/#operation/v2_order_limit_buy

print(get_response(action='v2/order/limit_buy', payload={
    'access_token': secret.ACCESS_TOKEN,
    'price': '1000',
    'qty': '100000000',
    'currency': 'LMCH',
}))
