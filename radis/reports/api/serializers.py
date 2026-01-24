from typing import Any

from django.db import transaction
from rest_framework import serializers, validators
from rest_framework.exceptions import ValidationError
from rest_framework.relations import PrimaryKeyRelatedField

from ..models import Language, Metadata, Modality, Report


class MetadataSerializer(serializers.ModelSerializer):
    class Meta:
        model = Metadata
        fields = ("key", "value")


class LanguageSerializer(serializers.ModelSerializer):
    class Meta:
        model = Language
        fields = ("code",)

    def run_validation(self, data: dict[str, Any]) -> Any:
        # We don't want to check if this modality already exists in the database
        # as we later use get_or_create.
        for validator in self.fields["code"].validators:
            if isinstance(validator, validators.UniqueValidator):
                self.fields["code"].validators.remove(validator)
        return super().run_validation(data)


class ModalitySerializer(serializers.ModelSerializer):
    class Meta:
        model = Modality
        fields = ("code",)

    def run_validation(self, data: dict[str, Any]) -> Any:
        # We don't want to check if this modality already exists in the database
        # as we later use get_or_create.
        for validator in self.fields["code"].validators:
            if isinstance(validator, validators.UniqueValidator):
                self.fields["code"].validators.remove(validator)
        return super().run_validation(data)


class ReportSerializer(serializers.ModelSerializer):
    language = LanguageSerializer()
    metadata = MetadataSerializer(many=True)
    modalities = ModalitySerializer(many=True)

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        if self.context.get("skip_document_id_unique"):
            self._strip_unique_validator("document_id")
        request = self.context.get("request")
        if request is not None and "groups" in self.fields:
            groups_field = self.fields["groups"]
            if isinstance(groups_field, PrimaryKeyRelatedField):
                if groups_field.queryset is not None:
                    if request.user.is_superuser:
                        groups_field.queryset = groups_field.queryset.all()
                    else:
                        groups_field.queryset = request.user.groups.all()

    class Meta:
        model = Report
        fields = "__all__"

    def _strip_unique_validator(self, field_name: str) -> None:
        field = self.fields.get(field_name)
        if not field:
            return
        field.validators = [
            validator
            for validator in field.validators
            if not isinstance(validator, validators.UniqueValidator)
        ]

    def create(self, validated_data: Any) -> Any:
        language = validated_data.pop("language")
        groups = validated_data.pop("groups")
        metadata = validated_data.pop("metadata")
        modalities = validated_data.pop("modalities")

        with transaction.atomic():
            language_instance, _ = Language.objects.get_or_create(**language)

            report = Report.objects.create(**validated_data, language=language_instance)

            report.groups.set(groups)

            for metadata in metadata:
                Metadata.objects.create(report=report, **metadata)

            modality_instances: list[Modality] = []
            for modality in modalities:
                modality_instance, _ = Modality.objects.get_or_create(**modality)
                modality_instances.append(modality_instance)

            report.modalities.set(modality_instances)

        return report

    def update(self, report: Report, validated_data: Any) -> Any:
        language = validated_data.pop("language")
        groups = validated_data.pop("groups")
        metadata = validated_data.pop("metadata")
        modalities = validated_data.pop("modalities")

        with transaction.atomic():
            language_instance = Language.objects.get(**language)
            report.language = language_instance

            for attr, value in validated_data.items():
                setattr(report, attr, value)

            report.save()

            report.groups.set(groups)

            report.metadata.all().delete()
            for metadata in metadata:
                Metadata.objects.create(report=report, **metadata)

            report.modalities.clear()
            modality_instances: list[Modality] = []
            for modality in modalities:
                modality_instance, _ = Modality.objects.get_or_create(**modality)
                modality_instances.append(modality_instance)
            report.modalities.set(modality_instances)

        return report

    def to_internal_value(self, data: Any) -> Any:
        if "language" in data:
            if not isinstance(data["language"], str):
                raise ValidationError("Invalid language type.")
            data["language"] = {"code": data["language"]}

        if "metadata" in data:
            if not isinstance(data["metadata"], dict):
                raise ValidationError("Invalid metadata type.")
            data["metadata"] = [
                {"key": key, "value": value} for key, value in data["metadata"].items()
            ]

        if "modalities" in data:
            if not isinstance(data["modalities"], list):
                raise ValidationError("Invalid modalities type.")
            data["modalities"] = [{"code": code} for code in data["modalities"]]

        return super().to_internal_value(data)

    def to_representation(self, instance: Any) -> Any:
        ret = super().to_representation(instance)

        if "language" in ret:
            ret["language"] = ret["language"]["code"]

        if "metadata" in ret:
            ret["metadata"] = {item["key"]: item["value"] for item in ret["metadata"]}

        if "modalities" in ret:
            ret["modalities"] = [item["code"] for item in ret["modalities"]]

        return ret

    def validate(self, attrs: Any) -> Any:
        if hasattr(self, "initial_data"):
            unknown_keys = set(self.initial_data.keys()) - set(self.fields.keys())
            if unknown_keys:
                raise ValidationError("Got unknown fields: {}".format(unknown_keys))
        return super().validate(attrs)
