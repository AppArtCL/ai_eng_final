from pinecone import Pinecone, ServerlessSpec
from app import config
import logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

pc = Pinecone(api_key=config.settings.pinecone_api_key.get_secret_value())

indices_existentes = [i["name"] for i in pc.list_indexes()]

if config.settings.pinecone_index_name not in indices_existentes:
    logging.info(f"Creando índice: {config.settings.pinecone_index_name}.")
    pc.create_index(
        name=config.settings.pinecone_index_name,
        dimension=config.settings.embedding_dim,
        metric=config.settings.pinecone_metric,
        spec=ServerlessSpec(cloud=config.settings.pinecone_cloud, region=config.settings.pinecone_region)
    )
    logging.info(f"Índice {config.settings.pinecone_index_name} creado exitosamente.")
else:
    dim_actual = pc.describe_index(config.settings.pinecone_index_name).dimension
    if dim_actual != config.settings.embedding_dim:
        logging.error(f"Dimensión del índice existente ({dim_actual}) no coincide con la configuración ({config.settings.embedding_dim}).")
        raise ValueError(f"Dimensión del índice existente ({dim_actual}) no coincide con la configuración ({config.settings.embedding_dim}).")
    logging.info(f"Usando índice existente: {config.settings.pinecone_index_name}.")

indice = pc.Index(config.settings.pinecone_index_name)
logging.info(f"Estado del índice {config.settings.pinecone_index_name}: {indice.describe_index_stats()}")
