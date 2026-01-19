import random

random.seed(12345)

STAGES = [
    ("common", 100, 499, 0.0, 0.0),
    ("rare", 500, 1999, 0.2, 0.0),
    ("epic", 2000, 4999, 0.4, 0.1),
    ("legend", 5000, 999999, 0.6, 0.15),
]

EVOLUTION_COSTS = {
    "rare": 1000,
    "epic": 3000,
    "legend": 5000,
}

ITEMS = {
    "104": {"name": "scratch_card", "price": 200, "type": "scratch_card", "awards": [
        {"name": "none", "prob": 0.45, "amount": 0},
        {"name": "small", "prob": 0.20, "amount": 20},
        {"name": "refund", "prob": 0.15, "amount": 100},
        {"name": "win", "prob": 0.10, "amount": 200},
        {"name": "lucky", "prob": 0.08, "amount": 500},
        {"name": "big", "prob": 0.018, "amount": 1000},
        {"name": "jackpot", "prob": 0.002, "amount": 2000},
    ]},
    "105": {"name": "snack", "price": 300, "type": "snack"},
    "107": {"name": "gene", "price": 2000, "type": "gene"},
    "108": {"name": "pandora", "price": 2000, "type": "pandora"},
    "109": {"name": "smuggle", "price": 5000, "type": "smuggle"},
}

INSTRUMENTS = {
    "F101": {"name": "bond", "price": 1.0},
}


def get_stage(value):
    for name, vmin, vmax, _, _ in STAGES:
        if vmin <= value <= vmax:
            return name
    return "common"


def get_bonuses(stage):
    for name, _, _, work_bonus, train_bonus in STAGES:
        if name == stage:
            return work_bonus, train_bonus
    return 0.0, 0.0


def compound_interest(principal, rate, hours):
    final_amount = principal * ((1 + rate) ** hours)
    return int(final_amount - principal)


class User:
    def __init__(self, uid):
        self.id = uid
        self.coins = 50000
        self.bank = 0
        self.bank_level = 1
        self.loan_amount = 0
        self.loan_principal = 0
        self.value = 300
        self.stage = get_stage(self.value)
        self.master = ""
        self.pets = []
        self.holdings = {}
        self.inventory = {}


def change_line(before, after):
    delta = after - before
    sign = "+" if delta >= 0 else ""
    return f"{before}->{after} ({sign}{delta})"


def log(lines, round_no, who, action, before, after, note=""):
    base = f"R{round_no} | {who} | {action} | coins {change_line(before, after)}"
    if note:
        base += f" | {note}"
    lines.append(base)


def purchase_pet(lines, round_no, users, buyer_id, target_id):
    buyer = users[buyer_id]
    target = users[target_id]
    cost = target.value
    before = buyer.coins
    buyer.coins -= cost
    buyer.pets.append(target_id)
    old_master = target.master
    target.master = buyer_id
    target.value += random.randint(10, 30)
    target.stage = get_stage(target.value)
    if not old_master:
        subsidy = cost // 2
        target_before = target.coins
        target.coins += subsidy
        log(lines, round_no, target_id, f"purchase_pet_subsidy from {buyer_id}",
            target_before, target.coins, f"amount={subsidy}")
        log(lines, round_no, buyer_id, f"purchase_pet {target_id}", before, buyer.coins,
            f"subsidy_to_pet={subsidy}")
    else:
        old = users[old_master]
        old_before = old.coins
        old.coins += cost
        if target_id in old.pets:
            old.pets.remove(target_id)
        log(lines, round_no, old_master, f"sell_pet {target_id}", old_before, old.coins)
        log(lines, round_no, buyer_id, f"purchase_pet {target_id}", before, buyer.coins)


def release_pet(lines, round_no, users, owner_id, target_id):
    owner = users[owner_id]
    target = users[target_id]
    refund = int(target.value * 0.3)
    before = owner.coins
    owner.coins += refund
    if target_id in owner.pets:
        owner.pets.remove(target_id)
    target.master = ""
    log(lines, round_no, owner_id, f"release_pet {target_id}", before, owner.coins,
        f"refund={refund}")


def ransom(lines, round_no, users, pet_id):
    pet = users[pet_id]
    if not pet.master:
        return
    master = users[pet.master]
    cost = pet.value
    pet_before = pet.coins
    master_before = master.coins
    pet.coins -= cost
    master.coins += cost
    if pet_id in master.pets:
        master.pets.remove(pet_id)
    pet.master = ""
    log(lines, round_no, pet_id, "ransom", pet_before, pet.coins, f"pay_to={master.id}:{cost}")
    log(lines, round_no, master.id, "ransom_receive", master_before, master.coins, f"from={pet_id}")


def work(lines, round_no, users, user_id):
    user = users[user_id]
    before = user.coins
    total = 0
    if not user.pets:
        total += random.randint(10, 50)
    else:
        for pid in user.pets:
            pet = users[pid]
            stage = pet.stage
            work_bonus, _ = get_bonuses(stage)
            if random.random() < 0.8:
                base = random.randint(20, 80) + pet.value // 10
                income = int(base * (1 + work_bonus))
                total += income
            else:
                loss = random.randint(10, 30)
                pet.value = max(100, pet.value - loss)
                pet.stage = get_stage(pet.value)
    if user.master and total > 0:
        tax = int(total * 0.3)
        net = total - tax
        master = users[user.master]
        master_before = master.coins
        master.coins += tax
        user.coins += net
        log(lines, round_no, user.master, "work_tax", master_before, master.coins, f"from={user_id}:{tax}")
    else:
        user.coins += total
    log(lines, round_no, user_id, "work", before, user.coins, f"gross={total}")


def train_pet(lines, round_no, users, owner_id, pet_id):
    owner = users[owner_id]
    pet = users[pet_id]
    cost = int(pet.value * 0.1)
    before = owner.coins
    owner.coins -= cost
    _, train_bonus = get_bonuses(pet.stage)
    success_rate = 0.7 + train_bonus
    if random.random() < success_rate:
        base = random.randint(15, 35)
        rate_inc = int(pet.value * 0.1)
        pet.value += base + rate_inc
        pet.stage = get_stage(pet.value)
    else:
        pet.value = max(100, pet.value - random.randint(10, 25))
        pet.stage = get_stage(pet.value)
    log(lines, round_no, owner_id, f"train {pet_id}", before, owner.coins, f"cost={cost}")


def batch_train(lines, round_no, users, owner_id):
    owner = users[owner_id]
    before = owner.coins
    total_cost = 0
    for pid in list(owner.pets):
        pet = users[pid]
        cost = int(pet.value * 0.1)
        if owner.coins < cost:
            break
        owner.coins -= cost
        total_cost += cost
        _, train_bonus = get_bonuses(pet.stage)
        success_rate = 0.7 + train_bonus
        if random.random() < success_rate:
            base = random.randint(15, 35)
            rate_inc = int(pet.value * 0.1)
            pet.value += base + rate_inc
        else:
            pet.value = max(100, pet.value - random.randint(10, 25))
        pet.stage = get_stage(pet.value)
    log(lines, round_no, owner_id, "batch_train", before, owner.coins, f"total_cost={total_cost}")


def evolve_pet(lines, round_no, users, owner_id, pet_id):
    owner = users[owner_id]
    pet = users[pet_id]
    before = owner.coins
    stage = pet.stage
    if stage == "common":
        need = 500
        next_stage = "rare"
    elif stage == "rare":
        need = 2000
        next_stage = "epic"
    elif stage == "epic":
        need = 5000
        next_stage = "legend"
    else:
        return
    if pet.value < need:
        return
    cost = EVOLUTION_COSTS[next_stage]
    owner.coins -= cost
    if random.random() < 0.8:
        pet.stage = next_stage
    else:
        pet.value = max(100, pet.value - int(pet.value * 0.1))
        pet.stage = get_stage(pet.value)
    log(lines, round_no, owner_id, f"evolve {pet_id}", before, owner.coins, f"cost={cost}")


def deposit(lines, round_no, users, user_id, amount, interest_hours=0):
    user = users[user_id]
    before = user.coins
    if interest_hours > 0 and user.bank > 0:
        interest = compound_interest(user.bank, 0.01, interest_hours)
        user.coins += interest
    user.coins -= amount
    user.bank += amount
    log(lines, round_no, user_id, f"deposit {amount}", before, user.coins, f"bank={user.bank}")


def withdraw(lines, round_no, users, user_id, amount, interest_hours=0):
    user = users[user_id]
    before = user.coins
    if interest_hours > 0 and user.bank > 0:
        interest = compound_interest(user.bank, 0.01, interest_hours)
        user.coins += interest
    user.bank -= amount
    user.coins += amount
    log(lines, round_no, user_id, f"withdraw {amount}", before, user.coins, f"bank={user.bank}")


def collect_interest(lines, round_no, users, user_id, hours):
    user = users[user_id]
    before = user.coins
    interest = compound_interest(user.bank, 0.01, hours) if user.bank > 0 else 0
    user.coins += interest
    log(lines, round_no, user_id, f"collect_interest {hours}h", before, user.coins, f"interest={interest}")


def upgrade_bank(lines, round_no, users, user_id):
    user = users[user_id]
    before = user.coins
    cost = int(100 * (1.5 ** (user.bank_level - 1)))
    user.coins -= cost
    user.bank_level += 1
    log(lines, round_no, user_id, "upgrade_bank", before, user.coins, f"cost={cost}")


def loan(lines, round_no, users, user_id, amount):
    user = users[user_id]
    before = user.coins
    user.loan_amount += amount
    user.loan_principal += amount
    user.coins += amount
    log(lines, round_no, user_id, f"loan {amount}", before, user.coins)


def repay(lines, round_no, users, user_id, amount):
    user = users[user_id]
    before = user.coins
    repay_amount = min(amount, user.loan_amount)
    user.coins -= repay_amount
    user.loan_amount -= repay_amount
    user.loan_principal = min(user.loan_principal, user.loan_amount)
    log(lines, round_no, user_id, f"repay {repay_amount}", before, user.coins)


def transfer(lines, round_no, users, sender_id, target_id, amount):
    sender = users[sender_id]
    target = users[target_id]
    fee = int(amount * 0.1)
    total = amount + fee
    sender_before = sender.coins
    target_before = target.coins
    sender.coins -= total
    target.coins += amount
    log(lines, round_no, sender_id, f"transfer {amount}->{target_id}", sender_before, sender.coins, f"fee={fee}")
    log(lines, round_no, target_id, f"transfer_recv {amount} from {sender_id}", target_before, target.coins)


def rob_success(lines, round_no, users, attacker_id, target_id):
    attacker = users[attacker_id]
    target = users[target_id]
    rate = random.randint(5, 20) / 100
    amount = int(target.coins * rate)
    attacker_before = attacker.coins
    target_before = target.coins
    target.coins -= amount
    attacker.coins += amount
    log(lines, round_no, attacker_id, f"rob_success {target_id}", attacker_before, attacker.coins, f"amount={amount}")
    log(lines, round_no, target_id, f"rob_loss {attacker_id}", target_before, target.coins, f"amount={amount}")


def rob_fail(lines, round_no, users, attacker_id):
    attacker = users[attacker_id]
    fine = int(attacker.value * 1.5)
    attacker.pending_fine = fine
    log(lines, round_no, attacker_id, "rob_fail", attacker.coins, attacker.coins, f"fine={fine}")


def pay_fine(lines, round_no, users, attacker_id):
    attacker = users[attacker_id]
    fine = getattr(attacker, "pending_fine", 0)
    before = attacker.coins
    attacker.coins -= fine
    attacker.pending_fine = 0
    log(lines, round_no, attacker_id, "pay_fine", before, attacker.coins, f"fine={fine}")


def buy_house(lines, round_no, users, user_id, price=20000):
    user = users[user_id]
    before = user.coins
    user.coins -= price
    log(lines, round_no, user_id, "buy_house", before, user.coins, f"price={price}")


def rent_house(lines, round_no, users, user_id, price=2000):
    user = users[user_id]
    before = user.coins
    user.coins -= price
    log(lines, round_no, user_id, "rent_house", before, user.coins, f"price={price}")


def buy_item(lines, round_no, users, user_id, item_id, count=1):
    user = users[user_id]
    item = ITEMS[item_id]
    total = item["price"] * count
    before = user.coins
    user.coins -= total
    user.inventory[item_id] = user.inventory.get(item_id, 0) + count
    log(lines, round_no, user_id, f"buy_item {item_id}x{count}", before, user.coins, f"cost={total}")


def use_item(lines, round_no, users, user_id, item_id, count=1):
    user = users[user_id]
    before = user.coins
    item = ITEMS[item_id]
    if item_id in user.inventory:
        user.inventory[item_id] -= count
        if user.inventory[item_id] <= 0:
            user.inventory.pop(item_id, None)
    if item["type"] == "scratch_card":
        total_win = 0
        for _ in range(count):
            r = random.random()
            cumulative = 0.0
            for award in item["awards"]:
                cumulative += award["prob"]
                if r < cumulative:
                    total_win += award["amount"]
                    break
        user.coins += total_win
        note = f"win={total_win}"
    elif item["type"] == "snack":
        pets = user.pets
        if pets:
            target = users[pets[0]]
            total_increase = 0
            for _ in range(count):
                total_increase += random.randint(20, 50)
            target.value += total_increase
            note = f"pet={target.id} value+={total_increase}"
        else:
            note = "no_pet"
    elif item["type"] == "gene":
        note = "no_coin_change"
    elif item["type"] == "pandora":
        total = 0
        for _ in range(count):
            r = random.random()
            if r < 0.08:
                user.coins += 20000
                total += 20000
            elif r < 0.30:
                user.coins += 4000
                total += 4000
            elif r < 0.80:
                loss = int(user.coins * 0.3)
                user.coins -= loss
                total -= loss
        note = f"delta={total}"
    elif item["type"] == "smuggle":
        total = 0
        for _ in range(count):
            if random.random() < 0.5:
                sale = random.randint(8000, 12000)
                user.coins += sale
                total += sale
            else:
                user.coins -= 2000
                total -= 2000
        note = f"delta={total}"
    else:
        note = ""
    log(lines, round_no, user_id, f"use_item {item_id}x{count}", before, user.coins, note)


def daily_checkin(lines, round_no, users, user_id):
    user = users[user_id]
    before = user.coins
    reward = random.randint(200, 500)
    user.coins += reward
    log(lines, round_no, user_id, "checkin", before, user.coins, f"reward={reward}")


def lottery(lines, round_no, users, user_id):
    user = users[user_id]
    before = user.coins
    price = 200
    user.coins -= price
    win_reds = sorted(random.sample(range(1, 34), 6))
    win_blue = random.randint(1, 16)
    bet_reds = sorted(random.sample(range(1, 34), 6))
    bet_blue = random.randint(1, 16)
    hit_red = len(set(bet_reds) & set(win_reds))
    hit_blue = 1 if bet_blue == win_blue else 0
    prize_mult = 0
    if hit_red == 6 and hit_blue == 1:
        prize_mult = 10000
    elif hit_red == 6:
        prize_mult = 1000
    elif hit_red == 5 and hit_blue == 1:
        prize_mult = 150
    elif (hit_red == 5) or (hit_red == 4 and hit_blue == 1):
        prize_mult = 10
    elif (hit_red == 4) or (hit_red == 3 and hit_blue == 1):
        prize_mult = 5
    elif hit_blue == 1:
        prize_mult = 3
    award = price * prize_mult
    user.coins += award
    log(lines, round_no, user_id, "lottery", before, user.coins, f"award={award}")


def buy_instrument(lines, round_no, users, user_id, code, amount):
    user = users[user_id]
    before = user.coins
    price = INSTRUMENTS[code]["price"]
    shares = amount / price
    user.coins -= amount
    holding = user.holdings.get(code, {"shares": 0.0, "total_cost": 0.0})
    holding["shares"] += shares
    holding["total_cost"] += amount
    user.holdings[code] = holding
    avg_cost = holding["total_cost"] / holding["shares"] if holding["shares"] > 0 else 0.0
    log(lines, round_no, user_id, f"buy_instrument {code}", before, user.coins,
        f"amount={amount} shares={shares:.2f} avg_cost={avg_cost:.2f}")


def sell_instrument(lines, round_no, users, user_id, code, amount=None, sell_all=False):
    user = users[user_id]
    before = user.coins
    holding = user.holdings.get(code)
    if not holding or holding["shares"] <= 0:
        return
    price = INSTRUMENTS[code]["price"]
    if sell_all or amount is None:
        shares = holding["shares"]
    else:
        shares = min(holding["shares"], amount / price)
    proceeds = shares * price
    share_ratio = shares / holding["shares"] if holding["shares"] > 0 else 0.0
    cost_reduction = holding["total_cost"] * share_ratio
    holding["shares"] -= shares
    holding["total_cost"] -= cost_reduction
    user.coins += int(proceeds)
    if holding["shares"] <= 0:
        user.holdings.pop(code, None)
    pnl = int(proceeds - cost_reduction)
    log(lines, round_no, user_id, f"sell_instrument {code}", before, user.coins,
        f"proceeds={int(proceeds)} pnl={pnl}")


def update_instrument_price(lines, round_no, code, new_price):
    old_price = INSTRUMENTS[code]["price"]
    INSTRUMENTS[code]["price"] = new_price
    lines.append(f"R{round_no} | market | update_price {code} {old_price}->{new_price}")


def main():
    users = {f"U{i}": User(f"U{i}") for i in range(1, 11)}

    # initial pet relations
    users["U4"].pets.append("U5")
    users["U5"].master = "U4"
    users["U6"].pets.extend(["U7", "U8"])
    users["U7"].master = "U6"
    users["U8"].master = "U6"
    users["U9"].pets.append("U10")
    users["U10"].master = "U9"

    # preset values and stages
    users["U2"].value = 300
    users["U2"].stage = get_stage(users["U2"].value)
    users["U3"].value = 450
    users["U3"].stage = get_stage(users["U3"].value)
    users["U5"].value = 350
    users["U5"].stage = get_stage(users["U5"].value)
    users["U7"].value = 1700
    users["U7"].stage = get_stage(users["U7"].value)
    users["U8"].value = 250
    users["U8"].stage = get_stage(users["U8"].value)
    users["U10"].value = 280
    users["U10"].stage = get_stage(users["U10"].value)

    logs = []

    # round 1
    purchase_pet(logs, 1, users, "U1", "U2")
    work(logs, 1, users, "U2")
    train_pet(logs, 1, users, "U4", "U5")
    purchase_pet(logs, 1, users, "U3", "U5")
    deposit(logs, 1, users, "U3", 5000)
    loan(logs, 1, users, "U4", 8000)
    transfer(logs, 1, users, "U4", "U6", 3000)
    buy_item(logs, 1, users, "U6", "105", 20)
    use_item(logs, 1, users, "U6", "105", 20)
    buy_item(logs, 1, users, "U5", "104", 1)
    use_item(logs, 1, users, "U5", "104", 1)
    buy_instrument(logs, 1, users, "U6", "F101", 5000)
    rob_success(logs, 1, users, "U7", "U8")
    buy_house(logs, 1, users, "U9")
    daily_checkin(logs, 1, users, "U10")
    lottery(logs, 1, users, "U3")

    logs.append("Round 1 summary:")
    for uid in sorted(users.keys()):
        logs.append(f"{uid} coins={users[uid].coins}")

    # round 2
    ransom(logs, 2, users, "U2")
    evolve_pet(logs, 2, users, "U6", "U7")
    batch_train(logs, 2, users, "U6")
    release_pet(logs, 2, users, "U3", "U5")
    collect_interest(logs, 2, users, "U3", 6)
    withdraw(logs, 2, users, "U3", 2000, interest_hours=0)
    upgrade_bank(logs, 2, users, "U3")
    repay(logs, 2, users, "U4", 2000)
    rob_fail(logs, 2, users, "U8")
    pay_fine(logs, 2, users, "U8")
    rent_house(logs, 2, users, "U9")
    buy_item(logs, 2, users, "U5", "108", 1)
    use_item(logs, 2, users, "U5", "108", 1)
    buy_item(logs, 2, users, "U6", "107", 1)
    use_item(logs, 2, users, "U6", "107", 1)
    buy_item(logs, 2, users, "U10", "109", 1)
    use_item(logs, 2, users, "U10", "109", 1)
    update_instrument_price(logs, 2, "F101", 1.2)
    sell_instrument(logs, 2, users, "U6", "F101", sell_all=True)

    logs.append("Round 2 summary:")
    for uid in sorted(users.keys()):
        logs.append(f"{uid} coins={users[uid].coins}")

    with open("simulation_report.txt", "w", encoding="utf-8") as f:
        f.write("\n".join(logs))


if __name__ == "__main__":
    main()
