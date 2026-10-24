"""Sistema RAG híbrido: combina BM25 (léxico) y Pinecone (semántico) con EnsembleRetriever.

Uso:
    python rag_system.py      # requiere OPENAI_API_KEY y PINECONE_API_KEY en .env

Salida de ejemplo (pesos 0.5/0.5, obtenida en la Pre-entrega 4):

    Consulta: ¿Puedo tener mascota?
      1. convivencia_privada-001    | Tenencia y Regulación de Animales Domésticos o Mascotas
      2. asados-001                 | Regulación Exhaustiva sobre Asados y Tipo de Parrillas
      3. obras_mudanzas-005         | Gastos Comunes, Prorrateo Centralizado y Corte de Suministro
      4. convivencia_privada-003    | Normas de Convivencia, Emisiones de Humo y Ruido
      5. obras_mudanzas-004         | Régimen de Estacionamientos de Visitas y Circulaciones Viales

    Consulta: ¿Qué dice el reglamento sobre las UTM?
      1. convivencia_privada-004    | Procedimiento Sancionatorio y Régimen de Multas
      2. convivencia_privada-002    | Destino Exclusivo Habitacional y Prohibición Comercial
      3. convivencia_privada-000    | Introducción
      4. asados-003                 | Régimen de Terrazas, Balcones y Preservación de Fachadas
      5. convivencia_privada-001    | Tenencia y Regulación de Animales Domésticos o Mascotas

    Consulta: Departamentos 101 y 104:
      1. obras_mudanzas-001         | Normas sobre Reformas Interiores y Prohibición de Modificar Pisos
      2. asados-004                 | Polígonos de Uso y Goce Exclusivo en Cubiertas y Jardines
      3. convivencia_privada-002    | Destino Exclusivo Habitacional y Prohibición Comercial
      4. asados-002                 | Prevención de Incendios y Materiales Inflamables
      5. asados-001                 | Regulación Exhaustiva sobre Asados y Tipo de Parrillas

    Consulta: ¿Puedo hacer asados?
      1. convivencia_privada-003    | Normas de Convivencia, Emisiones de Humo y Ruido
      2. asados-001                 | Regulación Exhaustiva sobre Asados y Tipo de Parrillas
      3. obras_mudanzas-005         | Gastos Comunes, Prorrateo Centralizado y Corte de Suministro
      4. asados-000                 | Introducción
      5. obras_mudanzas-004         | Régimen de Estacionamientos de Visitas y Circulaciones Viales

Notas sobre la salida:
    - "mascota" y "UTM" aciertan en 1.er lugar gracias a preprocesar() (retrievers.py).
    - "101 y 104" es ambigua: ambos chunks del top-2 mencionan esos departamentos.
    - En "asados", BM25 da a "hacer" el mismo peso que a "asado" (ambas aparecen en un solo chunk),
      por lo que "Humo y Ruido" sube al 1.er lugar; el chunk correcto queda 2.º.
"""
import logging
from app import config
from app.rag.ingest import cargar_chunks
from app.rag.retrievers import crear_bm25, crear_vectorial
from langchain_core.documents import Document
from langchain_core.retrievers import BaseRetriever
from langchain_classic.retrievers import EnsembleRetriever

logger = logging.getLogger(__name__)

class RAGSystem:
    # bm25 y vectorial se pueden inyectar ya construidos (tests offline, comparación de pesos en evaluate.py).
    # Si no se pasan, se crean aquí.
    def __init__(
        self,
        k: int = config.settings.rag_top_k,
        pesos: tuple[float, float] = (config.settings.rag_peso_bm25, 1 - config.settings.rag_peso_bm25),
        bm25: BaseRetriever | None = None,
        vectorial: BaseRetriever | None = None,
    ):
        self.k = k
        self.pesos = pesos
        self.bm25 = bm25 or crear_bm25(cargar_chunks(), k)
        self.vectorial = vectorial or crear_vectorial(k)
        self.retriever_hibrido = EnsembleRetriever(
            retrievers=[self.bm25, self.vectorial],
            weights=list(pesos),
            id_key="chunk_id",
        )

    # Devuelve los top-k chunks combinando resultados léxicos y semánticos.
    def retrieve(self, query: str) -> list[Document]:
        documentos = self.retriever_hibrido.invoke(query)[:self.k]
        return documentos
    

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
    rag = RAGSystem()
    consultas = [
        "¿Puedo tener mascota?",
        "¿Qué dice el reglamento sobre las UTM?",
        "Departamentos 101 y 104:",
        "¿Puedo hacer asados?"
    ]
    for consulta in consultas:
        print(f"\nConsulta: {consulta}")
        for posicion, doc in enumerate(rag.retrieve(consulta), start=1):
            print(f"  {posicion}. {doc.metadata['chunk_id']:<26} | {doc.metadata['seccion']}")
