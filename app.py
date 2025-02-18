# app.py
import os
import openai
from langchain_openai import OpenAI, ChatOpenAI
from langchain.agents import Tool, AgentExecutor, OpenAIFunctionsAgent 
from langchain.prompts import StringPromptTemplate
from langchain.chains import LLMChain
from langchain.memory import ConversationBufferMemory
from langchain.schema import AgentAction, AgentFinish
from typing import List, Union, Dict
import requests
from flask import Flask, request, jsonify, render_template
from flask_sqlalchemy import SQLAlchemy
from langchain_community.llms import OpenAI
from langchain.chains import ConversationChain
import re
from dotenv import load_dotenv
from datetime import datetime, timedelta
from langchain_community.tools.tavily_search import TavilySearchResults
import logging

logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)

# 加载环境变量
load_dotenv()

# 确保API密钥被正确设置
openai_api_key = os.environ.get("OPENAI_API_KEY")
if not openai_api_key:
    raise ValueError("没有找到OPENAI_API_KEY环境变量")

# 使用API密钥初始化OpenAI
llm = ChatOpenAI(temperature=0, model="gpt-3.5-turbo", openai_api_key=openai_api_key)

# 设置Tavily API密钥
TAVILY_API_KEY = os.environ.get("TAVILY_API_KEY")
db = SQLAlchemy()
# 定义数据库模型
class ChatMessage(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    role = db.Column(db.String(10), nullable=False)
    content = db.Column(db.Text, nullable=False)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {
            'role': self.role,
            'content': self.content,
            'timestamp': self.timestamp.isoformat()
        }

# 初始化 Tavily 搜索工具
tavily_tool = TavilySearchResults(tavily_api_key=TAVILY_API_KEY)

# 定义搜索工具
tools = [
    Tool(
        name="Internet_Search",
        func=tavily_tool.run,
        description="This tool performs internet searches to find up-to-date information on any topic, including current events, financial data, and stock market news. Use this tool whenever you need to access recent information or specific data that you don't have."
    )
]

# 定义提示模板
template = """你是一个专业的金融顾问助手。使用以下工具来回答用户的问题：

{tools}

使用以下格式：

人类: 人类的输入问题
思考: 你应该总是思考下一步该做什么
行动: 工具名称 -> 输入工具的参数
观察: 工具的输出
... (这个思考/行动/观察可以重复多次)
思考: 我现在知道了最终答案
最终答案: 给人类的最终答案

开始！

人类: {input}
{agent_scratchpad}"""

# 创建提示模板
class CustomPromptTemplate(StringPromptTemplate):
    template: str
    tools: List[Tool]
    
    def format(self, **kwargs) -> str:
        intermediate_steps = kwargs.pop("intermediate_steps")
        thoughts = ""
        for action, observation in intermediate_steps:
            thoughts += action.log
            thoughts += f"\n观察: {observation}\n思考: "
        kwargs["agent_scratchpad"] = thoughts
        kwargs["tools"] = "\n".join([f"{tool.name}: {tool.description}" for tool in self.tools])
        return self.template.format(**kwargs)

prompt = CustomPromptTemplate(
    template=template,
    tools=tools,
    input_variables=["input", "intermediate_steps"]
)

# 定义输出解析器
class CustomOutputParser:
    def parse(self, llm_output: str) -> Union[AgentAction, AgentFinish]:
        if "最终答案:" in llm_output:
            return AgentFinish(
                return_values={"output": llm_output.split("最终答案:")[-1].strip()},
                log=llm_output,
            )
        
        action_match = re.search(r"行动: (\w+) -> (.+)", llm_output, re.DOTALL)
        if action_match:
            action = action_match.group(1)
            action_input = action_match.group(2)
            return AgentAction(tool=action, tool_input=action_input.strip(" ").strip('"'), log=llm_output)
        
        return AgentFinish(
            return_values={"output": "无法确定下一步行动。请提供更多信息或重新表述你的问题。"},
            log=llm_output,
        )

# 初始化LLM和Agent
llm_chain = LLMChain(llm=llm, prompt=prompt)
agent = OpenAIFunctionsAgent.from_llm_and_tools(llm=llm, tools=tools)

memory = ConversationBufferMemory(memory_key="chat_history")
agent_executor = AgentExecutor.from_agent_and_tools(
    agent=agent, 
    tools=tools, 
    verbose=True, 
    memory=memory
)

app = Flask(__name__)
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///chat.db'
db.init_app(app)

with app.app_context():
    db.create_all()

@app.route('/')
def home():
    return render_template('index.html')

@app.route('/chat', methods=['POST'])
def chat():
    user_message = request.json['message']
    logger.debug(f"Received user message: {user_message}")
    
    # 保存用户消息到数据库
    user_msg = ChatMessage(role='user', content=user_message)
    db.session.add(user_msg)
    db.session.commit()
    
    # 获取最近的对话记录（最多五条）
    one_day_ago = datetime.utcnow() - timedelta(days=1)
    recent_messages = ChatMessage.query.filter(ChatMessage.timestamp >= one_day_ago).order_by(ChatMessage.timestamp.desc()).limit(5).all()
    recent_messages.reverse()  # 反转顺序以便从最早的开始
    context = "\n".join([f"{msg.role}: {msg.content}" for msg in recent_messages])
    
    # 将用户消息和上下文传递给 agent
    try:
        response = agent_executor.run(f"{context}\n用户: {user_message}")
        logger.debug(f"Agent response: {response}")
    except Exception as e:
        logger.error(f"Error in agent execution: {str(e)}")
        response = "抱歉，处理您的请求时出现了错误。我们正在努力解决这个问题。在此期间，您可以尝试重新表述您的问题，或者直接在浏览器中搜索相关信息。"
    
    # 保存AI回复到数据库
    ai_msg = ChatMessage(role='assistant', content=response)
    db.session.add(ai_msg)
    db.session.commit()
    
    return jsonify({
        'response': response,
        'timestamp': ai_msg.timestamp.isoformat()
    })

@app.route('/chat_history', methods=['GET'])
def get_chat_history():
    messages = ChatMessage.query.order_by(ChatMessage.timestamp).all()
    history = [msg.to_dict() for msg in messages]
    return jsonify(history)

@app.route('/initial_message', methods=['POST'])
def initial_message():
    intro = (
        "你好！我是你的专业金融助手。我可以帮助你解答各种金融问题，包括投资策略、理财建议、市场分析等。有什么我可以帮到你的吗？\n"
        "Hi! I am your professional financial assistant. I can help you with various financial questions, including investment strategies, financial advice, market analysis, etc. How may I assist you today?"
    )
    ai_msg = ChatMessage(role='assistant', content=intro)
    db.session.add(ai_msg)
    db.session.commit()
    return jsonify({
        'response': intro,
        'timestamp': ai_msg.timestamp.isoformat()
    })

@app.route('/summary_and_inquiry', methods=['POST'])
def summary_and_inquiry():
    # 获取最后几条消息
    last_messages = ChatMessage.query.order_by(ChatMessage.timestamp.desc()).limit(10).all()
    last_messages.reverse()
    
    # 检查是否有完整的对话（用户问题和bot回答）
    has_complete_conversation = False
    for i in range(len(last_messages) - 1):
        if last_messages[i].role == 'user' and last_messages[i+1].role == 'assistant':
            has_complete_conversation = True
            break
    
    if not has_complete_conversation:
        # 如果没有完整的对话，返回一个通用的问候语
        greeting = "有什么我可以帮到你的吗？"
        ai_msg = ChatMessage(role='assistant', content=greeting)
        db.session.add(ai_msg)
        db.session.commit()
        return jsonify({
            'response': greeting,
            'timestamp': ai_msg.timestamp.isoformat()
        })
    
    context = "\n".join([f"{msg.role}: {msg.content}" for msg in last_messages])
    
    prompt = f"""
    基于以下对话内容：

    {context}

    请用两句话总结上一轮对话，然后询问今天有什么可以帮忙的。
    """
    
    response = llm.predict(prompt)
    
    ai_msg = ChatMessage(role='assistant', content=response)
    db.session.add(ai_msg)
    db.session.commit()
    
    return jsonify({
        'response': response,
        'timestamp': ai_msg.timestamp.isoformat()
    })

@app.route('/clear_chat', methods=['POST'])
def clear_chat():
    ChatMessage.query.delete()
    db.session.commit()
    return jsonify({'status': 'success'})

def test_tavily_api():
    url = "https://api.tavily.com/search"
    params = {
        "api_key": TAVILY_API_KEY,
        "query": "美股今日要闻",
        "search_depth": "advanced",
    }
    response = requests.get(url, params=params)
    logger.debug(f"Tavily API response status: {response.status_code}")
    logger.debug(f"Tavily API response content: {response.text}")
    return response.json() if response.status_code == 200 else None

# 在 app.py 的某个地方调用这个函数进行测试
test_result = test_tavily_api()
logger.debug(f"Tavily API test result: {test_result}")

if __name__ == '__main__':
    app.run(debug=True)
