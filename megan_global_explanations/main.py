import os
import random
import logging
import traceback
import typing as t
from collections import defaultdict

import hdbscan
import numpy as np
import matplotlib.pyplot as plt
import visual_graph_datasets.typing as tv
from sklearn.metrics import pairwise_distances
from scipy.spatial.distance import cosine
from graph_attention_student.torch.megan import Megan
from graph_attention_student.utils import array_normalize
from visual_graph_datasets.processing.base import ProcessingBase

from megan_global_explanations.utils import NULL_LOGGER
from megan_global_explanations.utils import extend_graph_info
from megan_global_explanations.utils import TEMPLATE_ENV
from megan_global_explanations.utils import DEFAULT_CHANNEL_INFOS
from megan_global_explanations.prototype.optimize import genetic_optimize
from megan_global_explanations.prototype.optimize import embedding_distances_fitness_mse
from megan_global_explanations.gpt import query_gpt



def extract_concepts(model: Megan,
                     index_data_map: t.Dict[int, dict],
                     processing: ProcessingBase,
                     dataset_type: t.Literal['regresssion', 'classification'] = 'regression',
                     fidelity_threshold: float = 0.0,
                     min_samples: int = 0,
                     min_cluster_size: int = 0,
                     cluster_metric: str = 'manhattan',
                     cluster_selection_method: str = 'leaf',
                     channel_infos: t.Dict[int, dict] = DEFAULT_CHANNEL_INFOS,
                     sort_similarity: bool = True,
                     logger: logging.Logger = NULL_LOGGER,
                     ) -> t.Dict[int, dict]:
    """
    This function uses the given MEGAN ``model``, the dataset in the format of the given ``index_data_map`` 
    to extract the concept explanations based on the models latent space representations.
    
    The concepts are returned in the format of a list of dictionaries, where every dict contains all the 
    relevant information about the concept.
    
    **HOW IT WORKS**
    
    The concepts are identified as dense clusters in a MEGAN model's latent space of subgraph embedddings. These 
    clusters are found using the HDBSCAN clustering algorithm. The clustering is performed for each explanation
    channel separately. The elements of the dataset are filtered based on the fidelity of the explanations. Only
    elements with a fidelity above a certain threshold are considered for the clustering.
    
    :param model: The MEGAN model for which the concept explanations should be created.
    :param index_data_map: The index data map representation of the dataset based on which the concept clustering 
        should be performed. The clustering process will consider the entire dataset. However, elements with a low 
        fidelity value (according to the model's explanations) will be discarded. This is a visual graph dataset 
        representation, where the integer keys are the indices of the elements in the dataset and the corresponding 
        values are the visual graph element dicts (containing the full graph representation and the path to the 
        graph visualization image.)
    :param processing: The processing instance that is compatible with the gvien model and dataset. This processing 
        instance can be used to convert the domain string representation to a graph dict and to create graph
        visualizations.
    :param dataset_type: A literal that defines the type of dataset that is being processed. Is either regression or 
        classification. This is required because internally, some calculations (such as the definition of fidelity) 
        are different between the two types.
    :param fidelity_threshold: The minimal fidelity value that is required for a sample to be considered for the
        concept clustering. If the fidelity of an explanation is below the threshold, it is not considered during 
        the concept clustering.
    :param min_samples: The minimal number of samples that are required to form a cluster. This is a parameter of the
        HDBSCAN clustering algorithm. This parameters controls how "conservative" the clustering will be. If this 
        parameter is larger then the clustering is more conservative and likely results in less clusters.
    :param min_cluster_size: The minimal number of elements required to form a cluster. Every concept cluster that 
        is found as a result of the procedure will have at least this many members.
    :param cluster_metric: A string identifier that determines which metric is used for the density estimation 
        durign the clustring. Default is manhattan distance, as it is said to be better than euclidean distance 
        for high dimensional data. Best case would be cosine distance, but this is not supported by HDBSCAN.
    :param cluster_selection_method: A string identifier that determines how the clusters are selected from the
        condensed tree that is generated by the HDBSCAN algorithm. The default is "leaf" which means that the
        leaf clusters are selected. Other options are "eom" and "louvain".
    :param channel_infos: A dictionary that contains information about the channels of the model. The keys of this 
        dictionary are the channel indices and the values are dictionaries that contain the name and the color of
        the channel. If no information is given for a channel, the default values are used.
    :sort_similarity: A boolean flag that determines whether the concepts should be sorted by similarity. If this
        flag is set to True, the concepts will be sorted such that the most similar concepts are next to each other.
    :param logger: A logger object that is used to log the progress of the concept extraction process.
    
    
    :returns: A list of concept dicts, where each dict represents one concept explanations that was extracted 
        for the given model and dataset combination.
    """
    num_channels = model.num_channels

    # ~ updating the dataset
    # In the first step we put the entire dataset through the model to obtain all the model outputs for all 
    # the elements of the dataset. This includes the output predictions, the explanation masks, the fidelity 
    # values, but also the latent space representations of the explanation channels.
    
    indices = list(index_data_map.keys())
    graphs = [index_data_map[index]['metadata']['graph'] for index in indices]
    
    logger.info(f'running model forward pass for the dataset with {len(graphs)} elements...')
    infos = model.forward_graphs(graphs)
    devs = model.leave_one_out_deviations(graphs)
    
    # We are attaching all this additional information that we obtain from the dataset here as additional 
    # attributes of the graphs dict objects themselves so that later on all the necessary information can 
    # be accessed from those.
    for index, graph, info, dev in zip(indices, graphs, infos, devs):
        
        graph['graph_output'] = info['graph_output']
        # besides the raw output vector for the prediction, we also want to store the actual prediction 
        # outcome. This differs based on what kind of task we are dealing with here. 
        if dataset_type == 'regression':
            graph['graph_prediction'] = info['graph_output'][0]
        elif dataset_type == 'classification':
            graph['graph_prediction'] = np.argmax(info['graph_output'])
        
        # correspondingly, the calculation of the fidelity is also different for regression and classification
        graph['graph_deviation'] = dev
        if dataset_type == 'regression':
            graph['graph_fidelity'] = np.array([-dev[0, 0], +dev[0, 1]])
        elif dataset_type == 'classification':
            matrix = np.array(dev)
            graph['graph_fidelity'] = np.diag(matrix)
        
        # Also we want to store all the information about the explanations channels, which includes the 
        # explanations masks themselves, but also the embedding vectors  
        graph['graph_embeddings'] = info['graph_embedding']
        graph['node_importances'] = array_normalize(info['node_importance'])
        graph['edge_importances'] = array_normalize(info['edge_importance'])

    # ~ concept clustering
    
    # As the concepts are generated we are going to store them in this list. Each concept is essentially 
    # representated as a dictionary which has certain keys that describe some aspect of it.
    concepts: t.List[dict] = []
    
    logger.info('starting the concept clustering...')
    cluster_index: int = 0
    for channel_index in range(num_channels):
        
        logger.info(f'for channel {channel_index}')
        
        # The first thing we do is to filter the dataset so that we only have those elements that meet 
        # the given fidelity threshold. Only if samples show a certain minimal fidelity we can be sure that 
        # those explanations are actually meaningful for the predictions.
        
        # These indices are now the *dataset indices* so the indices are only valid if applied to the 
        # index data map!
        indices_channel = [index for index, graph in zip(indices, graphs) if graph['graph_fidelity'][channel_index] > fidelity_threshold]
        indices_channel = np.array(indices_channel)
        
        graphs_channel = [index_data_map[index]['metadata']['graph'] for index in indices_channel]
        
        # channel_embeddings: (num_graphs, embedding_dim)
        graph_embeddings_channel = np.array([graph['graph_embeddings'][:, channel_index] for graph in graphs_channel])
        clusterer = hdbscan.HDBSCAN(
            min_samples=min_samples,
            min_cluster_size=min_cluster_size,
            metric=cluster_metric,
            cluster_selection_method=cluster_selection_method,
        )
        labels = clusterer.fit_predict(graph_embeddings_channel)
        
        clusters = [label for label in set(labels) if label >= 0]
        num_clusters = len(clusters)
        logger.info(f'found {num_clusters} from {len(graph_embeddings_channel)} embeddings')
        
        for cluster_label in clusters:
            
            mask_cluster = (labels == cluster_label)
            indices_cluster = indices_channel[mask_cluster]
            
            graph_embeddings_cluster = graph_embeddings_channel[mask_cluster]
            cluster_centroid = np.mean(graph_embeddings_cluster, axis=0)
            
            elements_cluster = [index_data_map[index] for index in indices_cluster]
            graphs_cluster = [data['metadata']['graph'] for data in elements_cluster]
            index_tuples_cluster = [(index, channel_index) for index in indices_cluster]
            
            if dataset_type == 'regression':
                contribution_cluster = np.mean([graph['graph_deviation'][0, channel_index] for graph in graphs_cluster])
            elif dataset_type == 'classification':
                contribution_cluster = np.mean([graph['graph_deviation'][channel_index, channel_index] for graph in graphs_cluster])
                
            concept: dict = {
                'index': cluster_index,
                'channel_index': channel_index,
                'index_tuples': index_tuples_cluster,
                'embeddings': graph_embeddings_cluster,
                'centroid': cluster_centroid,
                'contribution': contribution_cluster,
                'elements': elements_cluster,
                'graphs': graphs_cluster,
                'name': channel_infos[channel_index]['name'],
                'color': channel_infos[channel_index]['color'],
            }
            concepts.append(concept)
            cluster_index += 1
            
            logger.info(f' * cluster {cluster_index}'
                        f' - {len(elements_cluster)} elements')
            
    if sort_similarity:
        
        logger.info('sorting the concepts by similarity...')
        concepts_sorted = []
        concept_index = 0
        for channel_index in range(num_channels):
            
            concepts_channel = [concept for concept in concepts if concept['channel_index'] == channel_index]
            
            # We will just randomly start with the first cluster and then iteratively traverse the list of 
            # the clusters by always selecting the next cluster according to which one is the closest to the 
            # the current one - out of the remaining clusters.
            concept = concepts_channel.pop(0)
            
            while len(concepts_channel) != 0:
                
                centroid = concept['centroid']
                centroid_distances = pairwise_distances(
                    np.expand_dims(centroid, axis=0),
                    [c['centroid'] for c in concepts_channel],
                    metric=cluster_metric,
                )
                
                index = np.argmin(centroid_distances[0])
                concept = concepts_channel.pop(index)
                concept['index'] = concept_index
                concept_index += 1
                
                concepts_sorted.append(concept)
                
        concepts = concepts_sorted
            
    return concepts
            
            
# Given the already clustered concepts, the model and the dataset, this function generates the prototypes for 
# those clusters by doing a genetic algorithm optimization to minimize the graph size while maintaining semantic 
# similarity to the concept cluster centroid.        
def generate_concept_prototypes(concepts: list[dict],
                                model: Megan,
                                processing: ProcessingBase,
                                index_data_map: dict[int, dict],
                                mutate_funcs: t.List[t.Callable],
                                initial_population_size: int = 10,
                                initial_strategy: t.Literal['random', 'centroid'] = 'centroid',
                                violation_radius: float = 0.2,
                                population_size: int = 500,
                                num_epochs: int = 25,
                                width: int = 1000,
                                height: int = 1000,
                                logger: logging.Logger = NULL_LOGGER,
                                path: str = os.getcwd(),
                                ):
    """
    This function extends a given list of ``concepts`` by optimizing the prototype graphs for those concepts.
    The optimized prototypes will be appended as a corresponding attribute to the concept dictionaries.
    
    **WHAT IS A PROTOTYPE?**
    
    One issue with concept explanations is that they represent the concept as a set of example elements. This 
    still placed the burden of recognizing the shared pattern on the human observer. To make this easier, a 
    prototype is a graph of minimal size that still represents the concept in a meaningful way. So the idea 
    is that the prototype graph only shows the essential pattern itself.
    
    **HOW IT WORKS**
    
    To generate a prototype graph for a given concept, an optimization procedure is used. Based on an initial
    population consisting of the members of the concept cluster, deletion based mutations are used to remove 
    elements from these graphs to minimize their graph size (node count) while constraining the resulting 
    embeddings (latent space representations) to be close to the concept centroid. Therefore this optimization 
    should yield a small graph which still contains all the necessary elements that are essential for concept 
    membership.
    
    :param concepts: This is the list of concept dicts which has previously been extracted using the concept
        clustering functionality. Each element in this list represents one concept explanation that has been 
        identified.
    :param model: This is the MEGAN model for which the concept explanations were initially created. This 
        model will be used to map the mutated graphs into the latent space to check their similarity to the
        concept cluster.
    :param processing: This has to be a valid processing instance which is compatible with the given model and 
        which is able to generate the correct graph representation given the string domain representation of 
        the graph.
    :param mutate_funcs: This is a list of callable objects where each callable implements a possible mutation 
        operation during the execution of the genetic algorithm. These callable objects receive as the single 
        arguments an element dict from the population (which consists of the "graph" dict representation and the 
        "value" string representation of the graph) and return a new element dict that is the result of the
        mutation operation. If more than one mutation operation is given in this list, the algorithm will select 
        a random one during each mutation.
    :param initial_population_size: This is the number of initial elements that are taken from the concept cluster 
        for the optimization process. Note that this number is different from the populations size of the genetic 
        algorithm itself!
    :param initial_strategy: This is the strategy that is used to select the initial population from the concept
        cluster. The two possible strategies are "random" and "centroid". The "random" strategy selects the initial 
        population randomly from the concept cluster, while the "centroid" strategy selects the initial population 
        based on the distance to the cluster centroid.
    :param violation_radius: This is the radius of the violation that is allowed for the optimization. The violation
        radius is the maximum distance that the optimized graph embeddings are allowed to have to the concept cluster 
        centroid. If the distance is larger than this radius, a large penalty term is applied to the fitness function
        of the corresponding element.
    :param population_size: This is the size of the population that is used in the genetic algorithm optimization.
    :param num_epochs: This is the number of epochs that the genetic algorithm optimization is run for.
    :param width: This is the width of the visualization images that are generated for the prototype graphs.
    :param height: This is the height of the visualization images that are generated for the prototype graphs.
    :param logger: This is the logger object that is used to log the progress of the optimization process.
    :param path: This is the path where the visualization images of the prototype graphs are saved to. The default
        value for this is the current working directory, but it is advised to set this to a propert path.
    
    :returns: None
    """
    
    logger.info('extending the graph information...')
    extend_graph_info(index_data_map)
    
    logger.info('starting the prototype generation...')
    for concept_info in concepts:
        
        logger.info(f' * concept {concept_info["index"]}...')
        concept_index = concept_info['index']
        concept_graphs = concept_info['graphs']
        concept_embeddings = concept_info['embeddings']
        concept_centroid = concept_info['centroid']
        
        # There could be the cases where the concept clusters are relatively small (consist of few elements)
        # and in these cases to avoid an error we cap the number of initial members as the total number of 
        # members
        num_initial = min(initial_population_size, len(concept_graphs))
        
        # For the random initial population strategy we just randomly sample the initial graphs from the
        # concept cluster.
        if initial_strategy == 'random':
            initial_graphs = random.sample(concept_graphs, k=num_initial)
            
        # For the centroid strategy we choose the num_initial elements that are closest to the cluster centroid
        elif initial_strategy == 'centroid':
            centroid_distances = np.array([cosine(concept_centroid, emb) for emb in concept_embeddings])
            indices = np.argsort(centroid_distances).tolist()[:num_initial]
            initial_graphs: list[dict] = [concept_graphs[index] for index in indices]
            
        logger.info(f'   created {len(initial_graphs)} initial elements with "{initial_strategy}" strategy')
        initial_elements: list[dict] = []
        for graph in initial_graphs:
            
            graph = graph.copy()
            
            del graph['node_importances']
            del graph['edge_importances']
            
            initial_elements.append({
                'value': graph['graph_repr'],
                'graph': graph,
            })
            
        element, history = genetic_optimize(
            fitness_func=lambda elements: embedding_distances_fitness_mse(
                elements=elements,
                model=model,
                channel_index=concept_info['channel_index'],
                anchors=[concept_centroid],
                violation_radius=violation_radius,
            ),
            mutation_funcs=mutate_funcs,
            population_size=population_size,
            num_epochs=num_epochs,
            # To populate the initial population we are going to use the initial_elements list that we 
            # have just constructed. If the pop size is larger than the number of initial elements (which is 
            # very likely) elements may be duplicated in the initial population.
            sample_func=lambda: random.choice(initial_elements),
            logger=logger,
        )
        prototype_graph = element['graph']
        prototype_value = element['value']
        logger.info(f'   optimized prototype with {len(prototype_graph["node_indices"])} nodes and value: {prototype_value}')
        
        # ~ updating prototype with predictions
        # Now we put the prototype graph through the model to obtain the model output and the explanation masks
        # and the graph embedding for it so we can update the graph dict representation with that additional 
        # information
        prototype_info = model.forward_graphs([prototype_graph])[0]
        prototype_graph['graph_prediction'] = prototype_info['graph_output']
        prototype_graph['graph_embeddings'] = prototype_info['graph_embedding']
        prototype_graph['node_importances'] = array_normalize(prototype_info['node_importance'])
        prototype_graph['edge_importances'] = array_normalize(prototype_info['edge_importance']) 
        
        # ~ Adding the prototype to the concept info
        # After the generation of the prototype graph is done, we still need to add that prototype to the 
        # concept dict such that it can be processed in subsequent steps. The important thing in this step 
        # is that a valid prototype specification for the concept dict is a visual graph element. Specifically, 
        # this means that it must include the path to a visualization image of the graph, which will first need 
        # to be created using the processing instance.
        
        image_path = os.path.join(path, f'prototype_{concept_info["index"]}.png')
        fig, node_positions = processing.visualize_as_figure(
            prototype_value, 
            graph=prototype_graph,
            width=width,
            height=height,    
        )
        prototype_graph['node_positions'] = node_positions
        fig.savefig(image_path)
        plt.close(fig)
        
        # Here we just construct the minimal structure of a visual graph element dict ourselves. This mainly 
        # means to add the graph dict to the metadata, but also to add the path to the visualization image.
        prototype = {
            'image_path': image_path,
            'metadata': {
                'graph':    prototype_graph,
                'repr':     prototype_value,
            },
        }
        # A concept can theoretically be associated with multiple prototypes, so here we add this list of prototypes 
        # if it does not already exist.
        if 'prototypes' not in concept_info:
            concept_info['prototypes'] = []
            
        concept_info['prototypes'].append(prototype)
        
    return concepts


def generate_concept_hypotheses(concepts: list[dict],
                                task_name: str,
                                task_description: str,
                                openai_key: str,
                                contribution_func: t.Callable[[int, float], str] = lambda cont: f'{cont:.2f}',
                                system_template: str = 'system_message_chemistry.j2',
                                user_template: str = 'user_message_chemistry.j2',
                                logger: logging.Logger = NULL_LOGGER,
                                ):
    """
    This function will 
    
    """
    
    system_temp = TEMPLATE_ENV.get_template(system_template)
    system_message = system_temp.render({'description': task_description})
    
    for concept in concepts:
        
        logger.info(f' * generating hypothesis for concept {concept["index"]}')

        # First of all we need to check whether the current concept has at least one prototype associated 
        # with it because the prototype is the means by which the concept pattern is communicated to the 
        # language model.
        if not 'prototypes' in concept or len(concept['prototypes']) == 0:
            logger.info(f'   concept has no prototype, skipping...')
            continue
    
        # ~ preparing the prompt
        # The language model prompt consists of two important parts: the system message and the user message.
        # The system message defines the generall task that the language model should perform. The user message 
        # is the specific evidence for the current concept.

        # This is supposed to be a string that encodes the information about *how* the concept impacts the 
        # property. For regression tasks, this is usually just the raw contribution value (average channel 
        # fidelity over all cluster members) while in classification this should be encoded into a more 
        # qualitative description "low", "medium", "high" etc.
        # The contribution_func is supposed to turn the raw numeric contribution value into this string
        contribution: str = contribution_func(concept['channel_index'], concept['contribution'])

        user_temp = TEMPLATE_ENV.get_template(user_template)
        user_message = user_temp.render({
            'prototypes': concept['prototypes'], 
            'name': task_name,
            'contribution': contribution,   
        })

        try:
            hypothesis, messages = query_gpt(
                api_key=openai_key,
                system_message=system_message,
                user_message=user_message,
            )
        except Exception as exc:
            logger.info(f'   error during hypothesis generation: {exc}')
            traceback.print_exc()
            continue
            
        # ~ updating the concept
        # Now that we have obtained the hypothesis string from the language model, we can add it to the 
        # concept dict so that it may be processed in the subsequent steps. Canonically, the hypothesis
        # is simply added to the concept dict as the additional "hypothesis" attribute
        logger.info(f'   obtained a hypothesis with {len(hypothesis)} characters')
        concept['hypothesis'] = hypothesis


    return concepts