from engine import *

app = Abroad()
sara, karim, amine = app.add_user("Sara (buyer)"), app.add_user("Karim (Insta seller)"), app.add_user("Amine (traveler)")

print("== Flow A: local prepaid order ==")
item = app.add_listing(karim, "Handmade leather bag", 120 * TND)
oid = app.create_local_order(sara, item); o = app.get(oid)
print("Order total:", fmt(o["total"]), "| platform fee:", fmt(o["fee"]))
code = app.fund(oid, "gw_001");      print("Paid -> escrow holds", fmt(app.balance("ESCROW")), "| delivery code:", code)
app.ship(oid, karim)
try: app.confirm_delivery(oid, "000000")
except EscrowError as e: print("Wrong code ->", e)
app.confirm_delivery(oid, code)
print("Released. Karim:", fmt(app.balance(f"USER:{karim}")), "| Platform:", fmt(app.balance("PLATFORM")), "| Escrow:", fmt(app.balance("ESCROW")))

print("\n== Flow B: traveler order (Paris -> Tunis) ==")
oid = app.create_traveler_order(sara, amine, "Nike sneakers from Zalando", 180 * TND, "FR", 1.5); o = app.get(oid)
print(f"Item {fmt(o['item_price'])} + traveler reward {fmt(o['reward'])} + service fee {fmt(o['fee'])} = {fmt(o['total'])}")
code = app.fund(oid, "gw_002"); app.ship(oid, amine); app.confirm_delivery(oid, code)
print("Amine receives:", fmt(app.balance(f"USER:{amine}")))

print("\n== Flow C: dispute -> refund ==")
oid = app.create_local_order(sara, item); app.fund(oid, "gw_003"); app.ship(oid, karim)
app.dispute(oid); app.resolve(oid, "buyer")
print("State:", app.get(oid)["state"], "| Sara refunded:", fmt(app.balance(f"USER:{sara}")))

print("\n== Pro Seller status ==")
print("Karim stats:", app.stats(karim), "| Pro:", app.is_pro(karim))
pro = app.add_user("Top seller")
app._q("UPDATE users SET completed=40, rating_sum=4.8*50, rating_count=50, disputes=0 WHERE id=?", pro)
l2 = app.add_listing(pro, "Sneakers", 120 * TND)
print("Fee on 120 TND: standard", fmt(app.local_fee(karim, 120*TND)), "vs Pro", fmt(app.local_fee(pro, 120*TND)))
