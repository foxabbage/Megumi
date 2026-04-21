# AI Vtuber

这是一个个人写的模块化AI Vtuber小项目，想法是做一个像Neuro那样的AI Vtuber，不过成熟程度远不及她。</br>
该项目通过core server，多进程以及websocket通讯实现不同组件之间的信息互通与串联。(尝试模仿openclaw的结构)</br>
已经完成并测试的部分:</br>

1. live_chat: 与人对话和实时回复直播
2. video: 自己操作浏览器完成一些简单的操作比如点开视频观看和发表评论

## demo

![demo video](video/demo.mp4)</br>
限于电脑内存不够，此处没有同时使用Live2D演示。

## 如何使用？

完成compenents/config_sample.py文件，重命名为config.py，安装requirements.txt中的依赖项，获取tokens，创建必需的文件夹，修改components/chat_llm.py对应启动参数和提示词，启动launcher_live.py/launcher_video.py
要添加组件和启用关闭也很简单：启动脚本可以指定启动的文件；通过继承components/base.py就很容易完成通讯模块，再实现自己需要的程序即可。

## TODO

1. Live2D: 只做了简单的函数提取动作。可以将音频通过VBcable导入VTS来达到更好的效果。另外有可能的话，可以参考最近的一些此方面的论文训练一个有关模型。
2. 增加AI回复的活人感。可以通过LLM生成数据集+SFT/RLHF。
3. 现在AI的gui操作只能点击一些体积极大的选项(ViT的gui操作就好比让大模型数清楚一个strawberry里有几个r一样随机)。如何改进？我能想到的一个方法是使用传统CNN预分割，像浏览器的gui处理那样。
4. 添加更多的工具与操作/游戏组件。游戏组件不局限于纯视觉。
5. 长期记忆只是简单的通过LLM提取后存储，可以使用mem0等替代。
