# Auto-generated. DO NOT EDIT MANUALLY — edit the YAML and re-generate.
# Case: 购物场景

from playwright.sync_api import sync_playwright, expect

def run(page):
    page.goto("http://localhost:4173/#/login")
    # Step 1: 在'请输入手机号'输入框输入：'15373137739'
    page.get_by_placeholder("请输入手机号").fill("15373137739")
    # Step 2: 在'请输入密码'输入框输入：'123456'
    page.get_by_role("textbox", name="请输入密码").fill("123456")
    # Step 3: 点击'登录'按钮
    page.get_by_role("button", name="登录").click()
    # Step 4: 断言页面包含文本 "15373137739"
    expect(None).to_contain_text("15373137739")
    # Step 5: 鼠标悬浮在页面右上角的用户菜单按钮 "15373137739" 上
    page.locator(".el-dropdown [role='button']").hover()
    # Step 6: 等待菜单中 "收货地址" 选项出现
    expect(page.get_by_role("menuitem", name="收货地址")).to_be_visible()
    # Step 7: 点击菜单中的 "收货地址" 选项
    page.get_by_role("menuitem", name="收货地址").click()
    # Step 8: 点击 "添加新地址" 按钮
    page.get_by_role("link", name="添加新地址").click()
    # Step 9: 在 "收件人" 输入框输入 "张三"
    page.get_by_placeholder("请输入收货人姓名").fill("张三")
    # Step 10: 在 "联系电话" 输入框输入 "15373137739"
    page.get_by_placeholder("请输入联系电话").fill("15373137739")
    # Step 11: 点击省份下拉框
    page.get_by_role("combobox").click()
    # Step 12: 在弹出选项中点击 "广东省"
    page.get_by_role("option", name="广东省").click()
    # Step 13: 点击城市下拉框
    page.get_by_role("combobox").click()
    # Step 14: 在弹出选项中点击 "深圳市"
    page.get_by_role("option", name="深圳市").click()
    # Step 15: 点击区县下拉框
    page.get_by_role("combobox").click()
    # Step 16: 在弹出选项中点击 "罗湖区"
    page.get_by_role("option", name="罗湖区").click()
    # Step 17: 在 "详细地址" 输入框输入 "城隍庙43号"
    page.get_by_placeholder("请输入详细地址，如街道、门牌号等").fill("城隍庙43号")
    # Step 18: 点击 "保存地址" 按钮
    page.get_by_role("button", name="保存地址").click()
    # Step 19: 点击 "全部商品分类"
    page.get_by_role("link", name="全部商品分类").click()
    # Step 20: 点击分类为 "家电 数码 手机"
    page.get_by_role("button", name="家电 数码 手机").click()
    # Step 21: 在商品预览页中筛选出的商品卡片中点击第一个 "小米 Redmi AirDots"
    page.get_by_text("小米 Redmi AirDots", exact=False).click()
    # Step 22: 在商品详情页点击 "加入购物车"
    page.get_by_role("button", name="加入购物车").click()
    # Step 23: 点击页面右上角的 "购物车" 图标
    page.get_by_role("link", name="购物车").click()
    # Step 24: 点击 "结算"
    page.get_by_role("button", name="结算 (1)").click()
    # Step 25: 点击 "提交订单"
    page.get_by_role("button", name="提交订单").click()
    # Step 26: 关闭 "选择支付方式"
    page.locator("div[role='dialog'][aria-label='选择支付方式'] button.el-dialog__headerbtn").click()
    # Step 27: 点击 "我的订单"
    page.get_by_role("link", name="我的订单").click()
    # Step 28: 点击 "待支付" 订单
    page.get_by_role("button", name="待支付").click()
    # Step 29: 点击 "立即支付"
    page.get_by_role("button", name="立即支付").click()
    # Step 30: 点击 "支付宝支付"
    page.get_by_text("支付宝支付", exact=True).click()
    # Step 31: 断言 "待支付" 不包含 "小米 Redmi AirDots"
    expect(page.locator(".flex.gap-8")).not_to_contain_text("小米 Redmi AirDots")

if __name__ == "__main__":
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        page = browser.new_page()
        run(page)
        browser.close()
