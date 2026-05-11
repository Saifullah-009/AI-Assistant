from fastapi import FastAPI, UploadFile, File
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel
from dotenv import load_dotenv
from langchain_groq import ChatGroq
from langchain_core.messages import HumanMessage, SystemMessage, AIMessage
from langchain_community.document_loaders import PyPDFLoader
from langchain_text_splitters import RecursiveCharacterTextSplitter
from langchain_community.embeddings import HuggingFaceEmbeddings
from langchain_community.vectorstores import Chroma
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.output_parsers import StrOutputParser
from datetime import datetime
import os
import shutil
import json

load_dotenv()

app = FastAPI()

# AI Model
model = ChatGroq(
    model="llama-3.1-8b-instant",
    api_key=os.getenv("GROQ_API_KEY"),
    temperature=0.7
)

# Embeddings
embeddings = HuggingFaceEmbeddings(
    model_name="sentence-transformers/all-MiniLM-L6-v2"
)

# Personalities
PERSONALITIES = {
    "professional": "You are a professional AI assistant. Be formal, precise and helpful. Use markdown formatting.",
    "friendly": "You are a friendly and casual AI assistant. Be warm and encouraging. Add emojis. Use markdown.",
    "teacher": "You are a patient teacher. Explain simply with examples. Use markdown headers and lists.",
    "motivator": "You are an energetic life coach. Be positive and inspiring. Use markdown for emphasis."
}

# Storage
conversations = {}
pdf_retriever = None
pdf_filename = None

# Models
class ChatMessage(BaseModel):
    user_id: str
    message: str
    personality: str = "professional"
    mode: str = "chat"
    conversation_id: str = "default"

class RestoreConversation(BaseModel):
    user_id: str
    conversation_id: str
    personality: str
    messages: list

class ClearChat(BaseModel):
    user_id: str
    conversation_id: str = None

# Routes
@app.get("/")
async def home():
    return FileResponse("static/index.html")

@app.post("/chat")
async def chat(data: ChatMessage):
    global pdf_retriever

    # PDF mode
    if data.mode == "pdf":
        if pdf_retriever is None:
            return {"reply": "Please upload a PDF first.", "mode": "pdf"}

        docs = pdf_retriever.invoke(data.message)
        context = "\n\n".join(doc.page_content for doc in docs)
        pages = list(set([doc.metadata.get("page", 0) + 1 for doc in docs]))

        prompt = ChatPromptTemplate.from_template("""
You are a helpful assistant answering questions about a document.
Use the context below to answer clearly using markdown formatting.
If the answer is not in the context, say "I couldn't find that in the document."

Context:
{context}

Question: {question}

Answer:
        """)

        chain = prompt | model | StrOutputParser()
        answer = chain.invoke({
            "context": context,
            "question": data.message
        })

        return {
            "reply": answer,
            "mode": "pdf",
            "pages": pages,
            "timestamp": datetime.now().isoformat()
        }

    # Chat mode - streaming
    key = f"{data.user_id}_{data.conversation_id}_{data.personality}"
    
    if key not in conversations:
        conversations[key] = [
            SystemMessage(content=PERSONALITIES.get(
                data.personality,
                PERSONALITIES["professional"]
            ))
        ]

    conversations[key].append(HumanMessage(content=data.message))

    async def stream_response():
        full_response = ""
        try:
            for chunk in model.stream(conversations[key]):
                if chunk.content:
                    full_response += chunk.content
                    yield f"data: {json.dumps({'token': chunk.content})}\n\n"
            
            conversations[key].append(AIMessage(content=full_response))
            yield f"data: {json.dumps({'done': True})}\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'error': str(e)})}\n\n"

    return StreamingResponse(
        stream_response(),
        media_type="text/event-stream"
    )

@app.post("/restore-conversation")
async def restore_conversation(data: RestoreConversation):
    """Restore conversation history to backend memory"""
    key = f"{data.user_id}_{data.conversation_id}_{data.personality}"
    
    # Rebuild conversation from messages
    conversations[key] = [
        SystemMessage(content=PERSONALITIES.get(data.personality, PERSONALITIES["professional"]))
    ]
    
    for msg in data.messages:
        if msg["role"] == "user":
            conversations[key].append(HumanMessage(content=msg["text"]))
        elif msg["role"] == "ai":
            conversations[key].append(AIMessage(content=msg["text"]))
    
    return {"status": "restored", "message_count": len(data.messages)}

@app.post("/upload-pdf")
async def upload_pdf(file: UploadFile = File(...)):
    global pdf_retriever, pdf_filename

    os.makedirs("uploads", exist_ok=True)
    pdf_path = "uploads/upload.pdf"
    
    with open(pdf_path, "wb") as f:
        shutil.copyfileobj(file.file, f)

    loader = PyPDFLoader(pdf_path)
    documents = loader.load()

    splitter = RecursiveCharacterTextSplitter(
        chunk_size=500,
        chunk_overlap=50
    )
    chunks = splitter.split_documents(documents)

    vectorstore = Chroma.from_documents(
        documents=chunks,
        embedding=embeddings
    )

    pdf_retriever = vectorstore.as_retriever(
        search_type="mmr",
        search_kwargs={"k": 5, "fetch_k": 10}
    )

    pdf_filename = file.filename

    return {
        "message": "PDF processed successfully",
        "filename": file.filename,
        "pages": len(documents),
        "chunks": len(chunks)
    }

@app.post("/clear")
async def clear(data: ClearChat):
    """Clear conversation from backend memory"""
    if data.conversation_id:
        # Clear specific conversation
        keys = [k for k in conversations if f"{data.user_id}_{data.conversation_id}_" in k]
    else:
        # Clear all conversations for user
        keys = [k for k in conversations if k.startswith(data.user_id)]
    
    for k in keys:
        del conversations[k]
    
    return {"status": "cleared", "count": len(keys)}

app.mount("/static", StaticFiles(directory="static"), name="static")