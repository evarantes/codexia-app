import os
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

class EmailService:
    def __init__(self):
        # Em produção, você usaria variáveis de ambiente para SMTP
        self.smtp_server = "smtp.gmail.com" 
        self.smtp_port = 587
        self.sender_email = "codexia.system@gmail.com"
        self.password = "mock_password"

    def send_delivery_email(self, to_email, customer_name, book_title, download_link):
        """
        Simula o envio do ebook.
        """
        subject = f"Seu livro '{book_title}' chegou! 📚"
        
        body = f"""
        Olá, {customer_name}!
        
        Obrigado por comprar '{book_title}'. Estamos muito felizes em tê-lo conosco.
        
        Aqui está o link para baixar seu livro:
        {download_link}
        
        Boa leitura!
        
        Atenciosamente,
        Equipe Codexia
        """
        
        print(f"\n[EMAIL MOCK] Enviando para: {to_email}")
        print(f"[EMAIL MOCK] Assunto: {subject}")
        print(f"[EMAIL MOCK] Corpo: {body}\n")
        
        # TODO: Implementar envio real via SMTP quando o usuário fornecer credenciais
        return True

    def send_remarketing_email(self, to_email, customer_name, book_title, discount_code):
        """
        Simula envio de oferta para clientes antigos.
        """
        subject = f"Oferta Especial: Novo livro com desconto! 🎁"
        
        body = f"""
        Olá, {customer_name}!
        
        Como você já é nosso leitor, preparamos algo especial.
        
        O livro '{book_title}' está com 20% OFF para você!
        Use o cupom: {discount_code}
        
        Aproveite agora!
        
        Atenciosamente,
        Equipe Codexia
        """
        
        print(f"\n[EMAIL MOCK - REMARKETING] Enviando para: {to_email}")
        print(f"[EMAIL MOCK] Assunto: {subject}")
        print(f"[EMAIL MOCK] Corpo: {body}\n")
        
        return True

    def send_password_reset_email(self, to_email, token):
        """
        Simula envio de email de recuperação de senha.
        """
        subject = "Recuperação de Senha - Codexia"
        from app.config import BASE_URL
        reset_link = f"{BASE_URL}/reset-password.html?token={token}"
        
        body = f"""
        Olá!
        
        Recebemos uma solicitação para redefinir sua senha.
        
        Se foi você, clique no link abaixo para criar uma nova senha:
        {reset_link}
        
        Se não foi você, ignore este email.
        
        Atenciosamente,
        Equipe Codexia
        """
        
        print(f"\n[EMAIL MOCK - RESET] Enviando para: {to_email}")
        print(f"[EMAIL MOCK] Assunto: {subject}")
        print(f"[EMAIL MOCK] Corpo: {body}\n")
        
        return True
