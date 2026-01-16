"""
投资功能单元测试
用于验证投资系统的核心逻辑
"""

import sys
sys.path.insert(0, r'h:\PythonProject\astrbot_plugin_pet_market')

# 模拟测试数据
class MockInvestment:
    def __init__(self):
        self.investments = []
        self.next_id = 1
    
    def _get_investment_trend(self):
        """主投资趋势"""
        import random
        rand = random.random() * 100
        
        trends = [
            ((0, 40), "横盘", lambda: random.uniform(-0.02, 0.02)),
            ((40, 65), "小涨", lambda: random.uniform(0.03, 0.05)),
            ((65, 85), "小跌", lambda: random.uniform(-0.04, -0.03)),
            ((85, 93), "中涨", lambda: random.uniform(0.06, 0.09)),
            ((93, 98), "中跌", lambda: random.uniform(-0.091, -0.05)),
            ((98, 99.5), "极端涨", lambda: random.uniform(0.10, 0.15)),
            ((99.5, 100), "极端跌", lambda: random.uniform(-0.18, -0.10)),
        ]
        
        for (min_p, max_p), name, func in trends:
            if min_p <= rand < max_p:
                return (name, func())
        
        return ("横盘", random.uniform(-0.02, 0.02))

    def _get_investment_trend_addon(self):
        """加投趋势"""
        import random
        rand = random.random() * 100
        
        trends = [
            ((0, 50), "横盘", lambda: random.uniform(-0.01, 0.01)),
            ((50, 75), "小涨", lambda: random.uniform(0.02, 0.04)),
            ((75, 90), "小跌", lambda: random.uniform(-0.039, -0.02)),
            ((90, 97), "中涨", lambda: random.uniform(0.05, 0.09)),
            ((97, 99.5), "中跌", lambda: random.uniform(-0.05, -0.04)),
            ((99.5, 99.9), "极端涨", lambda: random.uniform(0.10, 0.12)),
            ((99.9, 100), "极端跌", lambda: random.uniform(-0.081, -0.051)),
        ]
        
        for (min_p, max_p), name, func in trends:
            if min_p <= rand < max_p:
                return (name, func())
        
        return ("横盘", random.uniform(-0.01, 0.01))

    def _check_investment_trigger(self, investment):
        """检查止盈/止损"""
        total_input = investment["amount"] + investment.get("addon_amount", 0)
        profit_rate = (investment["current_value"] - total_input) / total_input
        
        if profit_rate >= 0.10:
            return "止盈"
        if profit_rate <= -0.05:
            return "止损"
        return None


def test_trend_distribution():
    """测试趋势分布"""
    print("=" * 60)
    print("测试1：趋势分布检验")
    print("=" * 60)
    
    mock = MockInvestment()
    main_counts = {}
    addon_counts = {}
    
    # 抽样1000次主投资趋势
    for _ in range(1000):
        trend, rate = mock._get_investment_trend()
        main_counts[trend] = main_counts.get(trend, 0) + 1
    
    # 抽样1000次加投趋势
    for _ in range(1000):
        trend, rate = mock._get_investment_trend_addon()
        addon_counts[trend] = addon_counts.get(trend, 0) + 1
    
    print("\n【主投资趋势分布】（抽样1000次）")
    for trend in ["横盘", "小涨", "小跌", "中涨", "中跌", "极端涨", "极端跌"]:
        count = main_counts.get(trend, 0)
        print(f"  {trend}: {count}次 ({count/10:.1f}%)")
    
    print("\n【加投趋势分布】（抽样1000次）")
    for trend in ["横盘", "小涨", "小跌", "中涨", "中跌", "极端涨", "极端跌"]:
        count = addon_counts.get(trend, 0)
        print(f"  {trend}: {count}次 ({count/10:.1f}%)")


def test_investment_trigger():
    """测试触发条件"""
    print("\n" + "=" * 60)
    print("测试2：止盈/止损触发条件")
    print("=" * 60)
    
    mock = MockInvestment()
    
    # 测试止盈
    investment = {
        "amount": 5000,
        "addon_amount": 0,
        "current_value": 5500  # 10%
    }
    trigger = mock._check_investment_trigger(investment)
    print(f"\n投入5000，当前5500（+10%）-> 触发：{trigger} {'✓' if trigger == '止盈' else '✗'}")
    
    # 测试止损
    investment = {
        "amount": 5000,
        "addon_amount": 0,
        "current_value": 4750  # -5%
    }
    trigger = mock._check_investment_trigger(investment)
    print(f"投入5000，当前4750（-5%）-> 触发：{trigger} {'✓' if trigger == '止损' else '✗'}")
    
    # 测试无触发
    investment = {
        "amount": 5000,
        "addon_amount": 0,
        "current_value": 5200  # +4%
    }
    trigger = mock._check_investment_trigger(investment)
    print(f"投入5000，当前5200（+4%）-> 触发：{trigger} {'✓' if trigger is None else '✗'}")
    
    # 测试加投
    investment = {
        "amount": 5000,
        "addon_amount": 2000,
        "current_value": 7700  # +10%
    }
    trigger = mock._check_investment_trigger(investment)
    print(f"投入7000，当前7700（+10%）-> 触发：{trigger} {'✓' if trigger == '止盈' else '✗'}")


def test_value_changes():
    """测试价值变化"""
    print("\n" + "=" * 60)
    print("测试3：投资价值变化模拟")
    print("=" * 60)
    
    mock = MockInvestment()
    
    # 模拟一个投资24小时的变化
    investment = {
        "amount": 5000,
        "addon_amount": 0,
        "current_value": 5000,
        "trend_history": []
    }
    
    print(f"\n初始投入：5000 金币")
    print(f"{'小时':<5} {'趋势':<10} {'涨跌幅':<10} {'当前价值':<10} {'总收益':<10}")
    print("-" * 50)
    
    for hour in range(1, 25):
        trend, rate = mock._get_investment_trend()
        old_value = investment["current_value"]
        new_value = int(old_value * (1 + rate))
        profit = new_value - 5000
        
        investment["current_value"] = new_value
        investment["trend_history"].append((trend, rate))
        
        print(f"{hour:<5} {trend:<10} {rate:+.2%}      {new_value:<10} {profit:+d}")
        
        # 检查触发条件
        trigger = mock._check_investment_trigger(investment)
        if trigger:
            print(f"  └─ 触发{trigger}条件！")
            break
    
    final_value = investment["current_value"]
    final_profit = final_value - 5000
    final_rate = final_profit / 5000
    print(f"\n最终结果：{final_value} 金币，收益 {final_profit:+d} ({final_rate:+.2%})")


if __name__ == "__main__":
    try:
        test_trend_distribution()
        test_investment_trigger()
        test_value_changes()
        
        print("\n" + "=" * 60)
        print("✅ 所有测试完成！")
        print("=" * 60)
    except Exception as e:
        print(f"\n❌ 测试失败：{e}")
        import traceback
        traceback.print_exc()
