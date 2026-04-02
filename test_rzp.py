import os
from dotenv import load_dotenv
from cashfree_pg.api_client import Cashfree
from cashfree_pg.models.create_order_request import CreateOrderRequest
from cashfree_pg.models.customer_details import CustomerDetails
from cashfree_pg.models.order_meta import OrderMeta
from cashfree_pg.models.pay_order_request import PayOrderRequest
from cashfree_pg.models.pay_order_request_payment_method import PayOrderRequestPaymentMethod
from cashfree_pg.models.upi_payment_method import UPIPaymentMethod
from cashfree_pg.models.upi import Upi
import time

load_dotenv()

CLIENT_ID = os.getenv('CASHFREE_CLIENT_ID')
CLIENT_SECRET = os.getenv('CASHFREE_CLIENT_SECRET')
ENVIRONMENT = os.getenv('CASHFREE_ENVIRONMENT', 'sandbox')
API_VERSION = "2023-08-01"

env = Cashfree.XProduction if ENVIRONMENT == 'production' else Cashfree.XSandbox
client = Cashfree(XEnvironment=env, XClientId=CLIENT_ID, XClientSecret=CLIENT_SECRET)

try:
    print(f"Testing with Client ID: {CLIENT_ID[:8]}...")
    print(f"Environment: {ENVIRONMENT}")

    # Step 1: Create order
    order_id = f"test_order_{int(time.time())}"
    customer = CustomerDetails(customer_id="test_merchant", customer_phone="9999999999")
    order_meta = OrderMeta(return_url=f"https://example.com?order_id={order_id}")
    order_request = CreateOrderRequest(
        order_id=order_id,
        order_amount=10.0,
        order_currency="INR",
        customer_details=customer,
        order_meta=order_meta,
        order_note="Test payment - ₹10"
    )

    order_response = client.PGCreateOrder(API_VERSION, order_request)
    order_data = order_response.data
    print(f"Order created: {order_id}")
    print(f"Payment session ID: {order_data.payment_session_id}")

    # Step 2: Generate UPI QR
    upi_method = UPIPaymentMethod(upi=Upi(channel="qrcode"))
    pay_method = PayOrderRequestPaymentMethod(actual_instance=upi_method)
    pay_request = PayOrderRequest(
        payment_session_id=order_data.payment_session_id,
        payment_method=pay_method
    )

    pay_response = client.PGPayOrder(API_VERSION, pay_request)
    pay_data = pay_response.data
    print(f"QR generated! cf_payment_id: {pay_data.cf_payment_id}")
    print(f"Action: {pay_data.action}")
    if pay_data.data and pay_data.data.payload:
        qr_code = pay_data.data.payload.get("qrcode", "")
        print(f"QR code base64 length: {len(qr_code)}")
    print("\nSUCCESS!")

except Exception as e:
    import traceback
    print("FAILED!")
    traceback.print_exc()
