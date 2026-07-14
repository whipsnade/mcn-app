import re


class MockSmsAuthProvider:
    code = "000000"

    def request_code(self, phone: str) -> str:
        if re.fullmatch(r"1[3-9]\d{9}", phone) is None:
            raise ValueError("invalid_phone")
        return self.code

    def verify(self, phone: str, code: str) -> tuple[str, str]:
        self.request_code(phone)
        if code != self.code:
            raise ValueError("invalid_code")
        return phone, f"手机用户_{phone[-4:]}"


class MockWechatAuthProvider:
    def verify(self, mock_ticket: str) -> tuple[str, str]:
        if mock_ticket != "mock-wechat-authorized":
            raise ValueError("invalid_wechat_ticket")
        return "mock-wechat-user", "微信快捷登录用户"
