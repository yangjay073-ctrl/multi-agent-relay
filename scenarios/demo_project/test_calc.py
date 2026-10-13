"""demo_project 自测（stdlib assert 版）。当前 test_discount_negative 失败。"""
import calc

assert calc.discount(1) == 100, "单件原价"
assert calc.discount(9) == 900, "9 件不满减原价"
assert calc.discount(10) == 800, "满 10 件 8 折"
assert calc.discount(20) == 1600, "满 20 件 8 折"
assert calc.discount(0) == 0, "0 件为 0"
assert calc.discount(-3) == 0, "负数量应按 0 计价"   # <-- 当前失败：返回 -300
print("all tests passed")
