import json

from django.contrib.gis.geos import GEOSGeometry
from django.core.exceptions import PermissionDenied
from django.utils.html import strip_tags

from rest_framework import serializers
from rest_framework_gis import serializers as geoserializers

from core.exceptions import MalformedRequestData
from observationtypes.serializer import ObservationTypeSerializer
from observationtypes.models import ObservationType
from users.serializers import UserSerializer

from .models import Location, Observation, Comment, MediaFile, ImageFile


class LocationSerializer(geoserializers.GeoFeatureModelSerializer):
    class Meta:
        model = Location
        geo_field = 'geometry'
        fields = ('id', 'name', 'description', 'status', 'created_at')
        write_only_fields = ('status',)


class LocationContributionSerializer(serializers.ModelSerializer):
    class Meta:
        model = Location
        depth = 1
        fields = ('id', 'name', 'description', 'status', 'created_at')
        write_only_fields = ('status',)


class ObservationSerializer(serializers.ModelSerializer):
    creator = UserSerializer()
    updator = UserSerializer()
    category = serializers.SerializerMethodField('get_category')

    class Meta:
        model = Observation
        depth = 0
        fields = (
            'status', 'category', 'review_comment',
            'creator', 'updator', 'created_at', 'version'
        )

    def get_category(self, observation):
        return observation.observationtype.id


class ContributionSerializer(object):
    """
    Serializes and deserializes contribution object from and to its GeoJSON
    conterparts.
    """
    def __init__(self, instance=None, data=None, many=False, context=None):
        self.many = many
        self.context = context

        if self.many:
            self.instance = instance
        else:
            self.instance = self.restore_object(instance, data)

    @property
    def data(self):
        if self.many:
            features = [self.to_native_min(obj) for obj in self.instance]
            return {
                "type": "FeatureCollection",
                "features": features
            }

        return self.to_native(self.instance)

    def restore_location(self, instance=None, data=None, geometry=None):
        if instance is not None:
            if data is not None:
                instance.name = data.get('name') or instance.name
                instance.description = data.get('description') or instance.description
                private=data.get('private') or instance.private
                private_for_project=data.get('private_for_project') or instance.private_for_project

            if geometry is not None:
                if type(geometry) is not unicode:
                    geometry = json.dumps(geometry)
                
                instance.geometry=GEOSGeometry(geometry)

            instance.save()
            return instance
        else:
            if (data is not None) and ('id' in data):
                try:
                    return Location.objects.get_single(
                        self.context.get('user'),
                        self.context.get('project').id,
                        data.get('id')
                    )
                except PermissionDenied, error:
                    raise MalformedRequestData(error)
            else:
                name = None
                description = None
                private_for_project = None
                private = False

                if data is not None:
                    name = strip_tags(data.get('name'))
                    description = strip_tags(data.get('description'))
                    private = data.get('private')
                    private_for_project = data.get('private_for_project')

                return Location(
                    name=name,
                    description=description,
                    geometry=GEOSGeometry(json.dumps(geometry)),
                    private=private,
                    private_for_project=private_for_project,
                    creator=self.context.get('user')
                )

    def restore_object(self, instance=None, data=None):
        if data is not None:
            properties = data.get('properties')
            location = properties.get('location')
            attributes = properties.get('attributes')
            user = self.context.get('user')

            status = properties.pop('status', None)
            review_comment = properties.pop('review_comment', None)

            if instance is not None:
                self.restore_location(
                    instance.location,
                    data=data.get('properties').pop('location', None),
                    geometry=data.pop('geometry', None)
                )

                return instance.update(
                    attributes=attributes,
                    updator=user,
                    status=status,
                    review_comment=review_comment
                )
            else:
                project = self.context.get('project')

                try:
                    category = project.observationtypes.get(
                        pk=properties.pop('category'))
                except ObservationType.DoesNotExist:
                    raise MalformedRequestData('The category can not'
                                               'be used with the project or '
                                               'does not exist.')

                location = self.restore_location(
                    data=data.get('properties').pop('location', None),
                    geometry=data.get('geometry')
                )

                return Observation.create(
                    attributes=attributes,
                    creator=user,
                    location=location,
                    project=category.project,
                    observationtype=category,
                    status=status
                )
        else:
            return instance

    def to_native_base(self, obj):
        location = obj.location

        updator = None
        if obj.updator is not None: 
            updator = {
                'id': obj.updator.id,
                'display_name': obj.updator.display_name
            }

        json_object = {
            'id': obj.id,
            'type': 'Feature',
            'geometry': json.loads(obj.location.geometry.geojson),
            'properties': {
                'status': obj.status,
                'creator': {
                    'id': obj.creator.id,
                    'display_name': obj.creator.display_name
                },
                'updator': updator,
                'created_at': obj.created_at,
                'version': obj.version,
                'review_comment': obj.review_comment,
                'location': {
                    'id': location.id,
                    'name': location.name,
                    'description': location.description
                }
            },
            'isowner': obj.creator == self.context.get('user')
        }

        return json_object

    def to_native_min(self, obj):
        json_object = self.to_native_base(obj)
        json_object['category'] = {
            'id': obj.observationtype.id,
            'name': obj.observationtype.name,
            'description': obj.observationtype.description,
            'symbol': (obj.observationtype.symbol.url 
                       if obj.observationtype.symbol else None),
            'colour': obj.observationtype.colour
        }

        return json_object

    def to_native(self, obj):
        json_object = self.to_native_base(obj)

        category_serializer = ObservationTypeSerializer(
            obj.observationtype, context=self.context)
        json_object['category'] = category_serializer.data

        comment_serializer = CommentSerializer(
            obj.comments.filter(respondsto=None),
            many=True,
            context=self.context
        )
        json_object['comments'] = comment_serializer.data

        attributes = {}
        for field in obj.observationtype.fields.all():
            value = obj.attributes.get(field.key)
            if value is not None:
                attributes[field.key] = field.convert_from_string(value)

        json_object['properties']['attributes'] = attributes

        return json_object


class CommentSerializer(serializers.ModelSerializer):
    creator = UserSerializer()
    isowner = serializers.SerializerMethodField('get_is_owner')

    class Meta:
        model = Comment
        fields = ('id', 'text', 'creator', 'respondsto', 'created_at',
            'isowner')

    def to_native(self, obj):
        native = super(CommentSerializer, self).to_native(obj)
        native['responses'] = CommentSerializer(
            obj.responses.all(),
            many=True,
            context=self.context
        ).data

        return native

    def get_is_owner(self, comment):
        return comment.creator == self.context.get('user')


class FileSerializer(serializers.ModelSerializer):
    creator = UserSerializer()
    isowner = serializers.SerializerMethodField('get_is_owner')
    url = serializers.SerializerMethodField('get_url')

    class Meta:
        model = MediaFile
        fields = (
            'id', 'name', 'description', 'created_at', 'creator', 'isowner',
            'url'
        )

    def get_is_owner(self, obj):
        """
        Returns `True` if the user provided in the serializer context is the
        creator of this file
        """
        return obj.creator == self.context.get('user')

    def get_url(self, obj):
        """
        Return the url to access this file based on its file type
        """
        if isinstance(obj, ImageFile):
            return obj.image.url

