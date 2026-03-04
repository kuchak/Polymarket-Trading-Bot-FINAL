import re

with open('/Users/yy/polymarket/polymarket_trader.py', 'r') as f:
    code = f.read()

# FIX 1: Sizing — $20 floor, use remainder if low
old_sizing = """            bet_size = min(
                self.bankroll / 10,
                market_room,
                max_deploy,
                self.bankroll,
            )
            if bet_size < MIN_BET:
                continue"""

new_sizing = """            bet_size = min(
                max(self.bankroll / 10, 20),
                market_room,
                max_deploy,
                self.bankroll,
            )
            if self.bankroll < 5:
                continue
            if bet_size > self.bankroll:
                bet_size = self.bankroll
            if bet_size < 5:
                continue"""

code = code.replace(old_sizing, new_sizing)

# FIX 2: Remove "skip if in open_positions" — API exit handles ALL positions
old_skip = """            if token_id in self.open_positions:
                continue

            sell_price = min(cur_price, 0.99) if action == "SELL_WIN" else cur_price"""

new_skip = """            sell_price = min(cur_price, 0.99) if action == "SELL_WIN" else cur_price"""

code = code.replace(old_skip, new_skip)

# FIX 3: Handle resolved markets at <1c — write off instead of retrying
old_action = """            action = None
            if cur_price >= 0.99:
                action = "SELL_WIN"
            elif avg_price > 0 and cur_price <= avg_price - 0.15:
                action = "STOP_LOSS"

            if not action:
                continue"""

new_action = """            action = None
            if cur_price >= 0.99:
                action = "SELL_WIN"
            elif cur_price < 0.01:
                logger.info(f"  💀 WRITE-OFF | {outcome[:25]} | {size:.1f} shr — resolved loss, skipping")
                continue
            elif avg_price > 0 and cur_price <= avg_price - 0.15:
                action = "STOP_LOSS"

            if not action:
                continue"""

code = code.replace(old_action, new_action)

# FIX 4: Clean open_positions when API exit sells a tracked position
old_api_success = """            success = self._sell_position(token_id, sell_price, size, {"outcome": outcome})
            if success and success != "RESOLVED":
                revenue = size * sell_price
                cost = size * avg_price
                pnl = revenue - cost
                self.bankroll += revenue
                self.total_pnl += pnl"""

new_api_success = """            success = self._sell_position(token_id, sell_price, size, {"outcome": outcome})
            if success and success != "RESOLVED":
                revenue = size * sell_price
                cost = size * avg_price
                pnl = revenue - cost
                self.bankroll += revenue
                self.total_pnl += pnl
                if token_id in self.open_positions:
                    del self.open_positions[token_id]"""

code = code.replace(old_api_success, new_api_success)

with open('/Users/yy/polymarket/polymarket_trader.py', 'w') as f:
    f.write(code)

print("✅ Patch applied:")
print("  1. Sizing: $20 floor (bankroll/10 or $20, whichever is bigger)")
print("  2. API exit now sells ALL positions including tracked ones")  
print("  3. Write-off resolved losses at <1c (no more retry spam)")
print("  4. Cleans open_positions when API exit sells tracked positions")
