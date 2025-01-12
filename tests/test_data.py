import os
import pytest
import json
import tempfile
import visual_graph_datasets as t
import visual_graph_datasets.typing as tv

import numpy as np
from visual_graph_datasets.processing.colors import ColorProcessing
from visual_graph_datasets.processing.base import ProcessingBase
from visual_graph_datasets.util import dynamic_import
from megan_global_explanations.utils import safe_int
from megan_global_explanations.testing import create_mock_concepts
from megan_global_explanations.testing import MockModel
from megan_global_explanations.data import ConceptWriter
from megan_global_explanations.data import ConceptReader

from .util import ASSETS_PATH, ARTIFACTS_PATH
from .util import load_mock_clusters
from .util import load_mock_vgd


@pytest.mark.parametrize('num,dim,num_prototypes',[
    (3, 32, 0),
    (5, 64, 1),
])
def test_concept_reader_works(num, dim, num_prototypes):
    """
    The ``ConceptReader`` class is the counterpart to the ``ConceptWriter`` class. It is used to read concept data from the
    disk. This test checks if the concept reader works as expected using some sample cases and mostly surface level checks
    for the existence of files.
    """
    # We first of all need to create the concept clusters that we need to store and load
    concepts: t.List[dict] = load_mock_clusters(
        num_clusters=num,
        embedding_dim=dim,
        num_prototypes=num_prototypes,
    )
    model = MockModel(
        num_channels=2,
        embedding_dim=dim,
    )
    index_data_map: dict = load_mock_vgd()
    
    
    with tempfile.TemporaryDirectory() as tempdir:
        
        # Also before we can read the data, we need to actually write it to the disk first.
        # NOTE: This assumes that the ConceptWriter works as expected!
        processing = ColorProcessing()        
        writer = ConceptWriter(
            path=tempdir,
            model=model,
            processing=processing
        )
        writer.write(concepts)
            
        # To be constructed, the ConceptReader relies on 3 main components:
        # 1. The model instance, based on which it was initially created
        # 2. A visual graph dataset index_data_map of the dataset from which it was created
        # 3. The path to the actual concept clustering data on the disk
        reader = ConceptReader(
            path=tempdir,
            model=model,
            dataset=index_data_map,
        )
        concepts: t.List[dict] = reader.read()
        assert len(concepts) == num
        
        for concept in concepts:
            
            assert 'elements' in concept
            assert 'centroid' in concept
            
            for element in concept['elements']:
                assert 'image_path' in element
                assert 'index' in element['metadata']
                assert 'node_indices' in element['metadata']['graph']
                assert 'edge_indices' in element['metadata']['graph']

@pytest.mark.parametrize('num,dim,num_prototypes',[
    (3, 32, 0),
    (5, 64, 1),
])
def test_concept_writer_works(num, dim, num_prototypes):
    """
    The ConceptWriter class is used to write concept data to the disk. This test checks if the concept writer
    works as expected using some sample cases and mostly surface level checks for the existence of files.
    """
    # Before being able to write the concept data to the disk we actually need some concept data to 
    # begin with in the first place so this helper function will create a concept data list with 
    # mock entries.
    processing = ColorProcessing()
    
    concepts: t.List[dict] = load_mock_clusters(
        num_clusters=num,
        embedding_dim=dim,
        num_prototypes=num_prototypes,
    )
    
    model = MockModel(
        num_channels=2,
        embedding_dim=dim,
    )
    
    with tempfile.TemporaryDirectory() as tempdir:
        
        assert len(os.listdir(tempdir)) == 0 
        
        # Setting up the writer instance itself.
        writer = ConceptWriter(
            path=tempdir,
            model=model,
            processing=processing,    
        )
        
        writer.write(concepts)
        files = os.listdir(tempdir)
        print(f'files: {files}')
        
        metadata_path = os.path.join(tempdir, 'metadata.json')
        assert os.path.exists(metadata_path)
        model_path = os.path.join(tempdir, 'model.ckpt')
        assert os.path.exists(model_path)
        
        for file in files:
            folder_path = os.path.join(tempdir, file)
            if not os.path.isdir(folder_path):
                continue
            
            if num_prototypes > 0:
                prototypes_path = os.path.join(folder_path, 'prototypes')
                assert os.path.exists(prototypes_path)
                assert os.listdir(prototypes_path) != []
        
            metadata_path = os.path.join(folder_path, 'metadata.json')
            with open(metadata_path, mode='r') as file:
                content = file.read()
                metadata = json.loads(content)

        
@pytest.mark.parametrize('num,dim,num_prototypes',[
    (3, 32, 0),
    (5, 64, 1),   
])
def test_create_mock_concepts(num, dim, num_prototypes):
    """
    create_mock_concepts is itself a helper function for testing. It is used to create a list of mock concepts.
    this function tests if that creation works.
    """
    
    concept_data = load_mock_clusters(
        num_clusters=num,
        embedding_dim=dim,
        num_prototypes=num_prototypes,
    )
    
    assert isinstance(concept_data, list)
    assert len(concept_data) == num
    for concept in concept_data:
        assert 'centroid' in concept
        
        if num_prototypes:
            assert 'prototypes' in concept
            for prototype in concept['prototypes']:
                assert 'metadata' in prototype
                assert 'image_path' in prototype
                tv.assert_graph_dict(prototype['metadata']['graph'])