import os
import requests
import random

class StockService:
    def __init__(self):
        self.pexels_api_key = os.getenv('PEXELS_API_KEY')
        self.pixabay_api_key = os.getenv('PIXABAY_API_KEY')

    def search_image(self, query: str, orientation: str = "landscape"):
        """Search for stock images based on query"""
        # Pexels First
        if self.pexels_api_key:
            try:
                url = f"https://api.pexels.com/v1/search?query={query}&per_page=5&orientation={orientation}"
                headers = {"Authorization": self.pexels_api_key}
                response = requests.get(url, headers=headers)
                if response.status_code == 200:
                    data = response.json()
                    if data.get('photos'):
                        photo = random.choice(data['photos'])
                        return photo['src']['original']  # Return URL
            except Exception as e:
                print(f"Pexels Error: {e}")

        # Pixabay Fallback
        if self.pixabay_api_key:
            try:
                pixabay_orientation = "horizontal" if orientation == "landscape" else "vertical"
                url = f"https://pixabay.com/api/?key={self.pixabay_api_key}&q={query}&image_type=photo&orientation={pixabay_orientation}"
                response = requests.get(url)
                if response.status_code == 200:
                    data = response.json()
                    if data.get('hits'):
                        hit = random.choice(data['hits'])
                        return hit['largeImageURL']
            except Exception as e:
                print(f"Pixabay Error: {e}")

        return None

    def search_video(self, query: str, orientation: str = "landscape"):
        """Search for stock videos based on query (Future Implementation)"""
        # Similar logic for videos
        pass
