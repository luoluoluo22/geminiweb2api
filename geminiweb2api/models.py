from typing import List, Optional, Dict
from pydantic import BaseModel

class Image(BaseModel):
    url: str
    title: Optional[str] = None
    alt: Optional[str] = None

class WebImage(BaseModel):
    image: Image

class GeneratedImage(BaseModel):
    image: Image
    cookies: Optional[Dict[str, str]] = None

class Candidate(BaseModel):
    rcid: str
    text: str
    thoughts: Optional[str] = None
    web_images: List[WebImage] = []
    generated_images: List[GeneratedImage] = []

    def visuals(self) -> List[Image]:
        imgs = [w.image for w in self.web_images]
        imgs.extend([g.image for g in self.generated_images])
        return imgs

class ModelOutput(BaseModel):
    metadata: List[str] = []
    candidates: List[Candidate] = []
    chosen: int = 0

    @property
    def text(self) -> str:
        if not self.candidates:
            return ""
        return self.candidates[self.chosen].text
    
    @property
    def rcid(self) -> str:
        if not self.candidates:
            return ""
        return self.candidates[self.chosen].rcid
