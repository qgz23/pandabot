from nonebot import on_command, on_message
from nonebot.adapters.qq import MessageEvent

hello = on_command("hello", aliases={"你好", "hi"}, priority=5)

@hello.handle()
async def handle_first_receive(event: MessageEvent):
    await hello.send("你好呀小熊猫！有什么我可以帮到你的吗？")