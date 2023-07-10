import os
import random
from argparse import ArgumentParser
from datetime import datetime
from pathlib import Path

import datasets
import evaluate
import numpy as np
import pandas as pd
import torch
from datasets import load_dataset
from haystack.nodes import PromptNode
from loguru import logger
from transformers import (
    AutoModelForSequenceClassification,
    AutoTokenizer,
    DataCollatorWithPadding,
)
from transformers import TrainingArguments, Trainer

from ai_dataset_generator import DatasetGenerator
from ai_dataset_generator.prompts import GenerateUnlabeledDataPrompt, ClassLabelPrompt

BASEPATH = Path("evaluation/application_evaluation")
RESULTSPATH = BASEPATH / "results"
DATASETPATH = BASEPATH / "datasets"
BASEPATH.mkdir(parents=True, exist_ok=True)
RESULTSPATH.mkdir(parents=True, exist_ok=True)
DATASETPATH.mkdir(parents=True, exist_ok=True)


class ApplicationEvaluator:
    """
    Given a dataset (including train and test splits), evaluates the performance of LMs
    trained respectively on:
    * the entire train split
    * various sizes (100, 200, 500, 1000, 2000, etc.) of generated train datasets using
      our dataset generator
    """

    def __init__(self, dataset_train: datasets.Dataset, dataset_test: datasets.Dataset, run_type: str, arguments):
        assert len(arguments.input_variables) == 1, "currently only 1 input variable is supported"
        self.dataset_column_name_text = arguments.input_variables[0]
        self.dataset_column_name_target = arguments.target_variable
        self.lm_name = arguments.lm
        self.dataset_name = arguments.dataset
        self.device = torch.device(arguments.torch_device)
        if arguments.devmode:
            # for development: shorten the splits to k rows (disabled if None)
            self.dev_shorten_dataset_splits_to_k_rows = 10
        else:
            self.dev_shorten_dataset_splits_to_k_rows = None
        # pathname of the xlsx file to store the results

        self.results_pathname = RESULTSPATH / f"{self.dataset_name}.xlsx"
        self.run_type = run_type

        # create pandas dataframe or use existing one from disk
        try:
            self.df = pd.read_excel(self.results_pathname)
            logger.info("read {} results from {}", len(self.df), self.results_pathname)
        except FileNotFoundError:
            self.df = pd.DataFrame()
            logger.info("created new results dataframe")

        # datasets
        self.dataset_train = dataset_train
        self.dataset_test = dataset_test

        # get number of unique labels
        unique_labels = set(self.dataset_test[self.dataset_column_name_target])
        self.num_labels = len(unique_labels)
        logger.info("found {} labels in dataset {}: {}", self.num_labels, self.dataset_name, unique_labels)

        # initialize LM and its tokenizer
        self.tokenizer = AutoTokenizer.from_pretrained(self.lm_name)
        self.lm = AutoModelForSequenceClassification.from_pretrained(self.lm_name, num_labels=self.num_labels).to(
            self.device
        )
        logger.info("initialized LM {}", self.lm_name)

        # shorten dataset splits if necessary
        if self.dev_shorten_dataset_splits_to_k_rows is not None:
            self.dataset_train = self.dataset_train.select(
                range(min(self.dev_shorten_dataset_splits_to_k_rows, len(self.dataset_train)))
            )
            self.dataset_test = self.dataset_test.select(
                range(min(self.dev_shorten_dataset_splits_to_k_rows, len(self.dataset_test)))
            )
            logger.error(
                "--- !!! DEV !!!: shortened datasets to {} rows !!! ---",
                self.dev_shorten_dataset_splits_to_k_rows,
            )

        # tokenize datasets
        self.dataset_train_tokenized = self.tokenize_dataset(self.dataset_train)
        self.dataset_test_tokenized = self.tokenize_dataset(self.dataset_test)
        self.data_collator = DataCollatorWithPadding(tokenizer=self.tokenizer)
        logger.info("tokenized datasets", self.dataset_name)

        # create trainer
        training_args = TrainingArguments("trainer", use_mps_device=True)
        trainer = Trainer(
            self.lm,
            training_args,
            train_dataset=self.dataset_train_tokenized,
            data_collator=self.data_collator,
            tokenizer=self.tokenizer,
            compute_metrics=ApplicationEvaluator.compute_metrics,
        )

        # train LM on original dataset
        logger.info("training LM on original dataset {}", self.dataset_name)
        trainer.train()
        eval_results = trainer.evaluate(self.dataset_test_tokenized)
        self.add_evaluation_result(
            self.run_type,
            len(self.dataset_train_tokenized),
            len(self.dataset_test_tokenized),
            eval_results,
        )

    def tokenize_dataset(self, dataset):
        return dataset.map(
            ApplicationEvaluator._tokenize_function,
            batched=True,
            fn_kwargs={
                "tokenizer": self.tokenizer,
                "dataset_column_name_text": self.dataset_column_name_text,
            },
        )

    @staticmethod
    def _tokenize_function(example, tokenizer, dataset_column_name_text):
        return tokenizer(example[dataset_column_name_text], truncation=True)

    @staticmethod
    def compute_metrics(eval_preds):
        accuracy = evaluate.load("accuracy")
        f1 = evaluate.load("f1")
        precision = evaluate.load("precision")
        recall = evaluate.load("recall")
        logits, labels = eval_preds
        predictions = np.argmax(logits, axis=-1)

        return {
            "accuracy": accuracy.compute(predictions=predictions, references=labels),
            "f1": f1.compute(predictions=predictions, references=labels),
            "precision": precision.compute(predictions=predictions, references=labels),
            "recall": recall.compute(predictions=predictions, references=labels),
        }

    def add_evaluation_result(
        self,
        run_type: str,
        training_size: int,
        test_size: int,
        eval_results: dict,
    ):
        """
        Adds an evaluation result to the df and stores the df to disk as an Excel file.
        :param run_type:
        :param dataset_name:
        :param training_size:
        :param test_size:
        :param eval_results:
        :return:
        """
        self.df = pd.concat(
            [
                self.df,
                pd.DataFrame(
                    [
                        {
                            "dt_utc": datetime.utcnow(),
                            "run_type": run_type,
                            "training_size": training_size,
                            "test_size": test_size,
                            "accuracy": eval_results["eval_accuracy"]["accuracy"],
                            "f1": eval_results["eval_f1"]["f1"],
                            "precision": eval_results["eval_precision"]["precision"],
                            "recall": eval_results["eval_recall"]["recall"],
                        },
                    ]
                ),
            ],
            ignore_index=True,
        )

        self.df.to_excel(self.results_pathname, index=False)
        logger.info("saved {} results to {}", len(self.df), self.results_pathname.resolve())


def generate_unlabeled_data(fewshot_examples, arguments):
    prompt = GenerateUnlabeledDataPrompt(
        input_variables=arguments.input_variables,
        task_description=arguments.task_description_generate,
    )

    prompt_node = PromptNode(
        model_name_or_path=arguments.llm,
        api_key=os.environ.get("OPENAI_API_KEY"),
        max_length=arguments.max_generation_length,
    )
    generator = DatasetGenerator(prompt_node)
    generated_dataset = generator.generate(
        support_examples=fewshot_examples,
        prompt_template=prompt,
        max_prompt_calls=arguments.max_prompt_calls,
        support_examples_per_prompt=arguments.support_examples_per_prompt,
    )

    # add the empty target column with proper column type
    original_dtype = fewshot_examples.features[arguments.target_variable].dtype
    if original_dtype in ("float32", "float64"):
        placeholder_value = -100000.0
    elif original_dtype in ("int32", "int64"):
        placeholder_value = -100000
    elif original_dtype == "string":
        placeholder_value = "-100000"
    else:
        raise ValueError("unsupported dtype {} - please define a placeholder value", original_dtype)
    new_column = [placeholder_value] * len(generated_dataset)
    generated_dataset = generated_dataset.add_column(arguments.target_variable, new_column)

    logger.info("generated dataset {}", generated_dataset)

    return generated_dataset


def annotate_dataset(fewshot_examples, generated_unlabeled_dataset, arguments):
    idx2label = dict(enumerate(fewshot_examples.features[arguments.target_variable].names))

    prompt = ClassLabelPrompt(
        input_variables=arguments.input_variables,
        target_variable=arguments.target_variable,
        label_options=idx2label,
        task_description=arguments.task_description_annotate,
    )

    prompt_node = PromptNode(
        model_name_or_path=arguments.llm,
        api_key=os.environ.get("OPENAI_API_KEY"),
        max_length=arguments.max_generation_length,
    )
    generator = DatasetGenerator(prompt_node)
    generated_dataset = generator.generate(
        support_examples=fewshot_examples,  # from above
        unlabeled_examples=generated_unlabeled_dataset,
        prompt_template=prompt,  # from above
        max_prompt_calls=arguments.max_prompt_calls,  # max number of calls to the LLM
        support_examples_per_prompt=arguments.support_examples_per_prompt,  # number of support examples per prompt
    )
    logger.info("annotated dataset {}", generated_dataset)

    return generated_dataset


def get_original_dataset_splits(arguments):
    # load the dataset
    dataset = load_dataset(arguments.dataset)
    logger.info("loaded dataset {}", arguments.dataset)

    return dataset[arguments.split_train], dataset[arguments.split_test]


def generate_and_annotate_dataset(fewshot_examples, arguments):
    # generate unlabeled dataset using LLM
    generated_unlabeled_dataset = generate_unlabeled_data(fewshot_examples, arguments)

    # annotate unlabeled dataset using LLM
    generated_annotated_dataset = annotate_dataset(fewshot_examples, generated_unlabeled_dataset, arguments)

    return generated_annotated_dataset


def run(arguments):
    # get the original dataset and its splits
    dataset_train, dataset_test = get_original_dataset_splits(arguments)

    # train and test the original dataset
    ApplicationEvaluator(dataset_train, dataset_test, "original", arguments)

    # TODO we could also use our own generated examples as few shot examples in later
    #  iterations in the repeating process below
    # load the dataset split and some few shot examples
    fewshot_examples = dataset_train.select(random.sample(range(len(dataset_train)), arguments.num_fewshot_examples))

    # 1) generate a dataset and annotate it
    # 2) extend the overall generated dataset with the generated dataset from step 1)
    # 3) train and test the generated dataset
    # 4) repeat until the generated dataset has reached the desired size
    generated_annotated_dataset = None
    while generated_annotated_dataset is None or len(generated_annotated_dataset) < arguments.max_size_generated:
        if arguments.devmode:
            # pretend we are generating and annotating a new dataset by always loading
            # the same one from disk to save money (@Jonas: love you buddy)
            try:
                current_generated_annotated_dataset = datasets.load_from_disk(
                    DATASETPATH / "generated_annotated_dataset_imdb_20_dev.dataset"
                )
            except FileNotFoundError:
                raise FileNotFoundError("please run the script with --devmode=False once to generate the dataset, then rename it match the filename above")
        else:
            # generate a dataset and annotate it using the power of LLM
            current_generated_annotated_dataset = generate_and_annotate_dataset(fewshot_examples, arguments)

        # extend the overall dataset with the newly generated one
        if generated_annotated_dataset is None:
            generated_annotated_dataset = current_generated_annotated_dataset
        else:
            generated_annotated_dataset = datasets.concatenate_datasets(
                [generated_annotated_dataset, current_generated_annotated_dataset]
            )
        logger.info("extended generated dataset to size {}", len(generated_annotated_dataset))

        # save the generated dataset to disk
        generated_annotated_dataset.save_to_disk(
            DATASETPATH
            / f"generated_annotated_dataset_{arguments.dataset}_{len(generated_annotated_dataset)}.dataset",
        )

        # train and test the generated dataset
        ApplicationEvaluator(
            generated_annotated_dataset, dataset_test, f"generated_{len(generated_annotated_dataset)}", arguments
        )


if __name__ == "__main__":
    parser = ArgumentParser()
    parser.add_argument("--llm", type=str, default="text-davinci-003")
    parser.add_argument("--lm", type=str, default="bert-base-uncased")
    parser.add_argument("--max_generation_length", type=int, default=100)
    parser.add_argument("--task_description_generate", type=str, default="Generate similar texts.")
    parser.add_argument(
        "--task_description_annotate",
        type=str,
        default="Classify the review whether it's positive or negative: " "{label_options}.",
    )
    parser.add_argument("--dataset", type=str, default="imdb")
    parser.add_argument("--split_train", type=str, default="train")
    parser.add_argument("--split_test", type=str, default="test")
    # parser.add_argument(
    #    "--input_variables", type=str, nargs="+", default=["text"]
    # )  # Column names as they occur in the dataset
    parser.add_argument(
        "--input_variables", type=str, nargs="+", default=["text"]
    )  # Column names as they occur in the dataset
    parser.add_argument("--num_fewshot_examples", type=int, default=3)
    parser.add_argument("--max_prompt_calls", type=int, default=20)
    parser.add_argument("--support_examples_per_prompt", type=int, default=1)
    parser.add_argument("--target_variable", type=str, default="label")
    parser.add_argument("--torch_device", type=str, default="mps")
    parser.add_argument("--devmode", action="store_true", default=True)
    parser.add_argument("--max_size_generated", type=int, default=1000)
    args = parser.parse_args()
    run(args)