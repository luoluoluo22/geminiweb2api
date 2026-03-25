from typing import List, Optional
from .models import ModelOutput, Candidate
from .constants import Headers

class ChatSession:
    def __init__(self, client, model: str, gem_id: Optional[str] = None, metadata: Optional[List[str]] = None):
        self.client = client
        self.model = model
        self.gem_id = gem_id
        self.metadata = metadata or []
        self.last_output: Optional[ModelOutput] = None
        
    @property
    def rcid(self) -> Optional[str]:
        if self.metadata and len(self.metadata) > 2:
            return self.metadata[2]
        return None

    def send_message(self, prompt: str, files: List[str] = []) -> ModelOutput:
        output = self.client.generate_content(prompt, files, self.model, self.gem_id, self)
        self.last_output = output
        if output.metadata:
            self.metadata = output.metadata
        return output
