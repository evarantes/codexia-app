from app.database import SessionLocal, engine, Base
# Importar TODOS os modelos para garantir que o SQLAlchemy os reconheça
from app.models import Book, Post, Lead
from app.services.ai_generator import AIContentGenerator

def seed():
    print("Criando tabelas no banco de dados...")
    # Debug: ver quais tabelas foram encontradas
    print(f"Tabelas encontradas nos modelos: {Base.metadata.tables.keys()}")
    
    Base.metadata.create_all(bind=engine)
    
    db = SessionLocal()
    
    # 1. Criar o livro "A Hora da Virada"
    print("--- 1. Cadastrando o livro 'A Virada de ANA' ---")
    book = db.query(Book).filter(Book.title == "A Virada de ANA").first()
    if not book:
        book = Book(
            title="A Virada de ANA",
            author="Você",
            synopsis="Ana achou que tinha o controle de tudo, mas o destino tinha outros planos. Uma história sobre superação e recomeços.",
            price=29.90,
            payment_link="https://mercadopago.com.br/checkout/avirada"
        )
        db.add(book)
        db.commit()
        db.refresh(book)
        print(f"Livro cadastrado com ID: {book.id}")
    else:
        print("Livro já existe no sistema.")

    # 2. Simular a IA gerando um anúncio
    print("\n--- 2. Codexia IA analisando o livro... ---")
    ai = AIContentGenerator()
    
    styles = ["cliffhanger", "storytelling", "short_video"]
    
    for style in styles:
        print(f"\n[Gerando Anúncio estilo: {style.upper()}]")
        copy = ai.generate_ad_copy(book.title, book.synopsis, style)
        print("-" * 30)
        print(copy)
        print("-" * 30)

    print("\n--- Sistema Codexia pronto para conectar ao Facebook! ---")
    db.close()

if __name__ == "__main__":
    seed()
