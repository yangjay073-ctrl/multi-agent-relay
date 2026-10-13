"""示例项目：批发计价。含一个已知 bug（负数数量返回负总价），用于端到端验证。"""


def discount(qty: int) -> int:
    """批发价：单价 100 元；满 10 件整单 8 折；返回总价（元，整数）。

    bug：qty 为负数时返回负总价（见 test_calc.py 失败用例）。
    """
    if qty >= 10:
        return int(qty * 100 * 0.8)
    return qty * 100
